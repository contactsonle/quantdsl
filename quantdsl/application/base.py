import six
from eventsourcing.application.base import EventSourcingApplication

from quantdsl.domain.model.call_dependencies import register_call_dependencies
from quantdsl.domain.model.call_dependents import register_call_dependents
from quantdsl.domain.model.call_link import register_call_link
from quantdsl.domain.model.call_requirement import register_call_requirement, CallRequirement
from quantdsl.domain.model.call_result import register_call_result
from quantdsl.domain.model.contract_specification import register_contract_specification
from quantdsl.domain.model.contract_valuation import register_contract_valuation
from quantdsl.domain.model.dependency_graph import register_dependency_graph
from quantdsl.domain.model.market_calibration import register_market_calibration, compute_market_calibration_params
from quantdsl.domain.model.market_simulation import register_market_simulation, MarketSimulation
from quantdsl.domain.services.dependency_graphs import get_dependency_values, generate_dependency_graph
from quantdsl.domain.services.fixing_dates import regenerate_execution_order
from quantdsl.infrastructure.dependency_graph_subscriber import DependencyGraphSubscriber
from quantdsl.infrastructure.event_sourced_repos.call_dependencies_repo import CallDependenciesRepo
from quantdsl.infrastructure.event_sourced_repos.call_dependents_repo import CallDependentsRepo
from quantdsl.infrastructure.event_sourced_repos.call_link_repo import CallLinkRepo
from quantdsl.infrastructure.event_sourced_repos.call_requirement_repo import CallRequirementRepo
from quantdsl.infrastructure.event_sourced_repos.call_result_repo import CallResultRepo
from quantdsl.infrastructure.event_sourced_repos.contract_specification_repo import ContractSpecificationRepo
from quantdsl.infrastructure.event_sourced_repos.contract_valuation_repo import ContractValuationRepo
from quantdsl.infrastructure.event_sourced_repos.market_calibration_repo import MarketCalibrationRepo
from quantdsl.infrastructure.event_sourced_repos.market_simulation_repo import MarketSimulationRepo
from quantdsl.infrastructure.event_sourced_repos.simulated_price_repo import SimulatedPriceRepo
from quantdsl.infrastructure.simulation_subscriber import SimulationSubscriber
from quantdsl.semantics import DslExpression, DslNamespace, Module
from quantdsl.domain.services.parser import dsl_parse


class BaseQuantDslApplication(EventSourcingApplication):
    """

    Flow of user stories:

    Register contract specification (DSL text).  --> gives required market names
    Generate compile call dependency graph using contract specification (and observation time?).  --> gives required fixing times

    Register price histories.
    Generate market calibration for required market names using available price histories and observation time.

    Generate market simulation for required market names from market calibration, observation time, and fixing times.

    Evaluate contract given call dependency graph and market simulation.
    """

    def __init__(self):
        super(BaseQuantDslApplication, self).__init__()
        self.contract_specification_repo = ContractSpecificationRepo(event_store=self.event_store)
        self.contract_valuation_repo = ContractValuationRepo(event_store=self.event_store)
        self.market_calibration_repo = MarketCalibrationRepo(event_store=self.event_store)
        self.market_simulation_repo = MarketSimulationRepo(event_store=self.event_store)
        self.simulated_price_repo = SimulatedPriceRepo(event_store=self.event_store)
        self.call_requirement_repo = CallRequirementRepo(event_store=self.event_store)
        self.call_dependencies_repo = CallDependenciesRepo(event_store=self.event_store)
        self.call_dependents_repo = CallDependentsRepo(event_store=self.event_store)
        self.call_link_repo = CallLinkRepo(event_store=self.event_store)
        self.call_result_repo = CallResultRepo(event_store=self.event_store)
        self.simulation_subscriber = SimulationSubscriber(
            market_calibration_repo=self.market_calibration_repo,
            market_simulation_repo=self.market_simulation_repo
        )
        self.dependency_graph_subscriber = DependencyGraphSubscriber(
            contract_specification_repo=self.contract_specification_repo,
            call_dependencies_repo=self.call_dependencies_repo,
            call_dependents_repo=self.call_dependents_repo
        )

    def close(self):
        self.dependency_graph_subscriber.close()
        self.simulation_subscriber.close()
        super(BaseQuantDslApplication, self).close()


    # Todo: Register historical data.

    def compute_market_calibration_params(self, price_process_name, historical_data):
        """
        Returns market calibration params for given price process name and historical data.
        """
        return compute_market_calibration_params(price_process_name, historical_data)

    def register_contract_specification(self, specification):
        """
        The contract specification is a Quant DSL module.
        """
        return register_contract_specification(specification=specification)

    def register_market_calibration(self, price_process_name, calibration_params):
        """
        Calibration params result from fitting a model of market dynamics to historical data.
        """
        assert isinstance(price_process_name, six.string_types)
        assert isinstance(calibration_params, dict)
        return register_market_calibration(price_process_name, calibration_params)

    def register_market_simulation(self, market_calibration_id, market_names, fixing_dates, observation_date,
                                   path_count, interest_rate):
        """
        A market simulation has simulated prices at specified times across a set of markets.
        """
        return register_market_simulation(market_calibration_id, market_names, fixing_dates, observation_date,
                                          path_count, interest_rate)

    def register_dependency_graph(self, contract_specification_id):
        return register_dependency_graph(contract_specification_id)

    def register_call_requirement(self, call_id, dsl_source, effective_present_time):
        """
        A call requirement is a node of the dependency graph.
        """
        return register_call_requirement(
            call_id=call_id,
            dsl_source=dsl_source,
            effective_present_time=effective_present_time
        )

    def register_call_dependencies(self, call_id, dependencies):
        return register_call_dependencies(call_id=call_id, dependencies=dependencies)

    def register_call_dependents(self, call_id, dependents):
        return register_call_dependents(call_id=call_id, dependents=dependents)

    def register_call_link(self, link_id, call_id):
        return register_call_link(link_id, call_id)

    def generate_dependency_graph(self, contract_specification):
        return generate_dependency_graph(contract_specification, self.call_dependencies_repo,
                                         self.call_dependents_repo)

    def register_call_result(self, call_id, result_value):
        return register_call_result(call_id=call_id, result_value=result_value)

    def register_contract_valuation(self, dependency_graph_id):
        return register_contract_valuation(dependency_graph_id)

    def generate_contract_valuation(self, dependency_graph_id, market_simulation):
        assert isinstance(dependency_graph_id, six.string_types), dependency_graph_id
        assert isinstance(market_simulation, MarketSimulation)
        v = self.register_contract_valuation(dependency_graph_id)

        for call_id in regenerate_execution_order(dependency_graph_id, self.call_link_repo):

            call = self.call_requirement_repo[call_id]
            assert isinstance(call, CallRequirement)

            # Evaluate the call requirement.
            dependency_values = get_dependency_values(call_id, self.call_dependencies_repo, self.call_result_repo)

            # - parse the expr
            stubbed_module = dsl_parse(call.dsl_source)

            assert isinstance(stubbed_module, Module), "Parsed stubbed expr string is not a module: %s" % stubbed_module

            # - build a namespace from the dependency values
            dsl_locals = DslNamespace(dependency_values)

            # - compile the parsed expr
            dsl_expr = stubbed_module.body[0].reduce(dsl_locals=dsl_locals, dsl_globals=DslNamespace())
            assert isinstance(dsl_expr, DslExpression), dsl_expr

            # - evaluate the compiled expr
            first_market_name = market_simulation.market_names[0] if market_simulation.market_names else None
            evaluation_kwds = {
                'simulated_price_repo': self.simulated_price_repo,
                'simulation_id': market_simulation.id,
                'interest_rate': market_simulation.interest_rate,
                'present_time': call.effective_present_time or market_simulation.observation_date,
                'first_market_name': first_market_name,
            }
            result_value = dsl_expr.evaluate(**evaluation_kwds)

            # - store the result
            register_call_result(call_id=call_id, result_value=result_value)
