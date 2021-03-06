from collections import defaultdict
from quantdsl.domain.model.call_dependencies import CallDependencies, CallDependenciesRepository, \
    register_call_dependencies
from quantdsl.domain.model.call_dependents import CallDependentsRepository, register_call_dependents
from quantdsl.domain.model.call_link import register_call_link
from quantdsl.domain.model.call_requirement import StubbedCall, register_call_requirement
from quantdsl.domain.model.call_result import CallResult, CallResultRepository
from quantdsl.domain.model.contract_specification import ContractSpecification
from quantdsl.domain.model.dependency_graph import register_dependency_graph
from quantdsl.semantics import Module, DslNamespace, extract_defs_and_exprs, DslExpression, generate_stubbed_calls
from quantdsl.domain.services.parser import dsl_parse


def generate_dependency_graph(contract_specification, call_dependencies_repo, call_dependents_repo):

    assert isinstance(contract_specification, ContractSpecification)
    dsl_module = dsl_parse(dsl_source=contract_specification.specification)
    assert isinstance(dsl_module, Module)
    dsl_globals = DslNamespace()
    function_defs, expressions = extract_defs_and_exprs(dsl_module, dsl_globals)
    dsl_expr = expressions[0]
    assert isinstance(dsl_expr, DslExpression)
    dsl_locals = DslNamespace()

    leaf_call_ids = []
    all_dependents = defaultdict(list)
    # Generate stubbed call from the parsed DSL module object.
    for stub in generate_stubbed_calls(contract_specification.id, dsl_module, dsl_expr, dsl_globals, dsl_locals):
        assert isinstance(stub, StubbedCall)
        call_id = stub.call_id
        dsl_source = stub.dsl_source
        effective_present_time = stub.effective_present_time
        dependencies = stub.dependencies

        # Register the call requirements.
        register_call_requirement(call_id, dsl_source, effective_present_time)

        # Register the call dependencies.
        register_call_dependencies(call_id, dependencies)

        # Keep track of the leaves and the dependents.
        if len(dependencies) == 0:
            leaf_call_ids.append(call_id)
        else:
            for dependency_call_id in dependencies:
                all_dependents[dependency_call_id].append(call_id)

    # Register the call dependents.
    for call_id, dependents in all_dependents.items():
        register_call_dependents(call_id, dependents)
    register_call_dependents(contract_specification.id, [])
    # Generate and register the call order.
    link_id = contract_specification.id
    for call_id in generate_execution_order(leaf_call_ids, call_dependents_repo, call_dependencies_repo):
        register_call_link(link_id, call_id)
        link_id = call_id


def get_dependency_values(call_id, dependencies_repo, result_repo):
    assert isinstance(result_repo, CallResultRepository), result_repo
    dependency_values = {}
    stub_dependencies = dependencies_repo[call_id]
    assert isinstance(stub_dependencies, CallDependencies), stub_dependencies
    for stub_id in stub_dependencies:
        try:
            stub_result = result_repo[stub_id]
        except KeyError:
            raise
        else:
            assert isinstance(stub_result, CallResult), stub_result
            dependency_values[stub_id] = stub_result.result_value
    return dependency_values


def generate_execution_order(leaf_call_ids, call_dependents_repo, call_dependencies_repo):
    assert isinstance(call_dependents_repo, CallDependentsRepository)
    assert isinstance(call_dependencies_repo, CallDependenciesRepository)

    # Topological sort, using Kahn's algorithm.

    # Initialise set of nodes that have no outstanding dependencies with the leaf nodes.
    S = set(leaf_call_ids)
    removed_edges = defaultdict(set)
    while S:

        # Pick a node, n, that has zero outstanding dependencies.
        n = S.pop()

        # Yield node n.
        yield n

        # Get dependents, if any were registered.
        try:
            dependents = call_dependents_repo[n]
        except KeyError:
            continue

        # Visit the nodes that are dependent on n.
        for m in dependents:

            # Remove the edge n to m from the graph.
            removed_edges[m].add(n)

            # If there are zero edges to m that have not been removed, then we
            # can add m to the set of nodes with zero outstanding dependencies.
            for d in call_dependencies_repo[m]:
                if d not in removed_edges[m]:
                    break
            else:
                # Forget about removed edges to m.
                removed_edges.pop(m)

                # Add m to the set of nodes that have zero outstanding dependencies.
                S.add(m)
