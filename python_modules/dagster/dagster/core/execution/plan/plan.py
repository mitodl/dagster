from collections import OrderedDict, namedtuple

from dagster import check
from dagster.core.definitions import (
    GraphDefinition,
    IPipeline,
    InputDefinition,
    Solid,
    SolidDefinition,
    SolidHandle,
    SolidOutputHandle,
)
from dagster.core.definitions.dependency import DependencyStructure
from dagster.core.errors import DagsterExecutionStepNotFoundError, DagsterInvariantViolationError
from dagster.core.execution.context.system import AssetStoreContext
from dagster.core.execution.resolve_versions import (
    resolve_step_output_versions_helper,
    resolve_step_versions_helper,
)
from dagster.core.system_config.objects import EnvironmentConfig
from dagster.core.types.dagster_type import DagsterTypeKind
from dagster.core.utils import toposort

from .compute import create_compute_step
from .inputs import FromConfig, FromDefaultValue, FromMultipleSources, FromStepOutput, StepInput
from .objects import ExecutionStep, StepOutputHandle


class _PlanBuilder:
    """_PlanBuilder. This is the state that is built up during the execution plan build process.

    steps List[ExecutionStep]: a list of the execution steps that have been created.

    step_output_map Dict[SolidOutputHandle, StepOutputHandle]:  maps logical solid outputs
    (solid_name, output_name) to particular step outputs. This covers the case where a solid maps to
    multiple steps and one wants to be able to attach to the logical output of a solid during
    execution.
    """

    def __init__(self, pipeline, environment_config, mode, step_keys_to_execute):
        self.pipeline = check.inst_param(pipeline, "pipeline", IPipeline)
        self.environment_config = check.inst_param(
            environment_config, "environment_config", EnvironmentConfig
        )
        check.opt_str_param(mode, "mode")
        check.opt_list_param(step_keys_to_execute, "step_keys_to_execute", of_type=str)
        self.step_keys_to_execute = step_keys_to_execute
        self.mode_definition = (
            pipeline.get_definition().get_mode_definition(mode)
            if mode is not None
            else pipeline.get_definition().get_default_mode()
        )
        self._steps = OrderedDict()
        self.step_output_map = dict()
        self._seen_keys = set()

    @property
    def pipeline_name(self):
        return self.pipeline.get_definition().name

    def add_step(self, step):
        # Keep track of the step keys we've seen so far to ensure we don't add duplicates
        if step.key in self._seen_keys:
            keys = [s.key for s in self._steps]
            check.failed(
                "Duplicated key {key}. Full list seen so far: {key_list}.".format(
                    key=step.key, key_list=keys
                )
            )
        self._seen_keys.add(step.key)
        self._steps[step.solid_handle.to_string()] = step

    def add_steps(self, steps):
        for step in steps:
            self.add_step(step)

    def get_step_by_handle(self, handle):
        check.inst_param(handle, "handle", SolidHandle)
        return self._steps[handle.to_string()]

    def get_output_handle(self, key):
        check.inst_param(key, "key", SolidOutputHandle)
        return self.step_output_map[key]

    def set_output_handle(self, key, val):
        check.inst_param(key, "key", SolidOutputHandle)
        check.inst_param(val, "val", StepOutputHandle)
        self.step_output_map[key] = val

    def build(self):
        """Builds the execution plan.
        """
        pipeline_def = self.pipeline.get_definition()
        # Recursively build the execution plan starting at the root pipeline
        self._build_from_sorted_solids(
            pipeline_def.solids_in_topological_order, pipeline_def.dependency_structure
        )

        # Construct dependency dictionary
        deps = {step.key: set() for step in self._steps.values()}

        for step in self._steps.values():
            for step_input in step.step_inputs:
                deps[step.key].update(step_input.dependency_keys)

        step_dict = {step.key: step for step in self._steps.values()}

        step_keys_to_execute = self.step_keys_to_execute or [
            step.key for step in self._steps.values()
        ]

        check_asset_store_intermediate_storage(self.mode_definition, self.environment_config)

        return ExecutionPlan(
            self.pipeline,
            step_dict,
            deps,
            self.storage_is_persistent(),
            step_keys_to_execute,
            self.environment_config,
        )

    def storage_is_persistent(self):
        return self.mode_definition.get_intermediate_storage_def(
            self.environment_config.intermediate_storage.intermediate_storage_name
        ).is_persistent

    def _build_from_sorted_solids(
        self, solids, dependency_structure, parent_handle=None, parent_step_inputs=None
    ):
        for solid in solids:
            handle = SolidHandle(solid.name, parent_handle)

            ### 1. INPUTS
            # Create and add execution plan steps for solid inputs
            step_inputs = []
            for input_name, input_def in solid.definition.input_dict.items():
                step_input = get_step_input(
                    self,
                    solid,
                    input_name,
                    input_def,
                    dependency_structure,
                    handle,
                    parent_step_inputs,
                )

                # If an input with dagster_type "Nothing" doesnt have a value
                # we don't create a StepInput
                if step_input is None:
                    continue

                check.inst_param(step_input, "step_input", StepInput)
                step_inputs.append(step_input)

            ### 2a. COMPUTE FUNCTION
            # Create and add execution plan step for the solid compute function
            if isinstance(solid.definition, SolidDefinition):
                solid_compute_step = create_compute_step(
                    self.pipeline_name, self.environment_config, solid, step_inputs, handle
                )
                self.add_step(solid_compute_step)

            ### 2b. RECURSE
            # Recurse over the solids contained in an instance of GraphDefinition
            elif isinstance(solid.definition, GraphDefinition):
                self._build_from_sorted_solids(
                    solid.definition.solids_in_topological_order,
                    solid.definition.dependency_structure,
                    parent_handle=handle,
                    parent_step_inputs=step_inputs,
                )

            else:
                check.invariant(
                    False,
                    "Unexpected solid type {type} encountered during execution planning".format(
                        type=type(solid.definition)
                    ),
                )

            ### 3. OUTPUTS
            # Create output handles for solid outputs
            for name, output_def in solid.definition.output_dict.items():
                output_handle = solid.output_handle(name)

                # Punch through layers of composition scope to map to the output of the
                # actual compute step
                resolved_output_def, resolved_handle = solid.definition.resolve_output_to_origin(
                    output_def.name, handle
                )
                compute_step = self.get_step_by_handle(resolved_handle)
                self.set_output_handle(
                    output_handle,
                    StepOutputHandle.from_step(compute_step, resolved_output_def.name),
                )


def get_step_input(
    plan_builder, solid, input_name, input_def, dependency_structure, handle, parent_step_inputs
):
    check.inst_param(plan_builder, "plan_builder", _PlanBuilder)
    check.inst_param(solid, "solid", Solid)
    check.str_param(input_name, "input_name")
    check.inst_param(input_def, "input_def", InputDefinition)
    check.inst_param(dependency_structure, "dependency_structure", DependencyStructure)
    check.opt_inst_param(handle, "handle", SolidHandle)
    check.opt_list_param(parent_step_inputs, "parent_step_inputs", of_type=StepInput)

    solid_config = plan_builder.environment_config.solids.get(str(handle))
    if solid_config and input_name in solid_config.inputs:
        return StepInput(
            name=input_name,
            dagster_type=input_def.dagster_type,
            source=FromConfig(
                solid_config.inputs[input_name],
                dagster_type=input_def.dagster_type,
                input_name=input_name,
            ),
        )

    input_handle = solid.input_handle(input_name)
    if dependency_structure.has_singular_dep(input_handle):
        solid_output_handle = dependency_structure.get_singular_dep(input_handle)
        return StepInput(
            name=input_name,
            dagster_type=input_def.dagster_type,
            source=FromStepOutput(
                step_output_handle=plan_builder.get_output_handle(solid_output_handle),
                dagster_type=input_def.dagster_type,
                check_for_missing=False,
            ),
        )

    if dependency_structure.has_multi_deps(input_handle):
        solid_output_handles = dependency_structure.get_multi_deps(input_handle)
        return StepInput(
            name=input_name,
            dagster_type=input_def.dagster_type,
            source=FromMultipleSources(
                [
                    FromStepOutput(
                        step_output_handle=plan_builder.get_output_handle(solid_output_handle),
                        dagster_type=input_def.dagster_type.get_inner_type_for_fan_in(),
                        check_for_missing=True,
                    )
                    for solid_output_handle in solid_output_handles
                ]
            ),
        )

    if solid.container_maps_input(input_name):
        parent_name = solid.container_mapped_input(input_name).definition.name
        parent_inputs = {step_input.name: step_input for step_input in parent_step_inputs}
        if parent_name in parent_inputs:
            parent_input = parent_inputs[parent_name]
            return StepInput(
                name=input_name, dagster_type=input_def.dagster_type, source=parent_input.source,
            )

    if solid.definition.input_has_default(input_name):
        return StepInput(
            name=input_name,
            dagster_type=input_def.dagster_type,
            source=FromDefaultValue(solid.definition.default_value_for_input(input_name)),
        )

    # At this point we have an input that is not hooked up to
    # the output of another solid or provided via environment config.

    # We will allow this for "Nothing" type inputs and continue.
    if input_def.dagster_type.kind == DagsterTypeKind.NOTHING:
        return None

    # Otherwise we throw an error.
    raise DagsterInvariantViolationError(
        (
            "In pipeline {pipeline_name} solid {solid_name}, input {input_name} "
            "must get a value either (a) from a dependency or (b) from the "
            "inputs section of its configuration."
        ).format(
            pipeline_name=plan_builder.pipeline_name, solid_name=solid.name, input_name=input_name
        )
    )


class ExecutionPlan(
    namedtuple(
        "_ExecutionPlan",
        "pipeline step_dict deps steps artifacts_persisted step_keys_to_execute environment_config",
    )
):
    def __new__(
        cls,
        pipeline,
        step_dict,
        deps,
        artifacts_persisted,
        step_keys_to_execute,
        environment_config,
    ):
        missing_steps = [step_key for step_key in step_keys_to_execute if step_key not in step_dict]
        if missing_steps:
            raise DagsterExecutionStepNotFoundError(
                "Execution plan does not contain step{plural}: {steps}".format(
                    plural="s" if len(missing_steps) > 1 else "", steps=", ".join(missing_steps)
                ),
                step_keys=missing_steps,
            )
        return super(ExecutionPlan, cls).__new__(
            cls,
            pipeline=check.inst_param(pipeline, "pipeline", IPipeline),
            step_dict=check.dict_param(
                step_dict, "step_dict", key_type=str, value_type=ExecutionStep
            ),
            deps=check.dict_param(deps, "deps", key_type=str, value_type=set),
            steps=list(step_dict.values()),
            artifacts_persisted=check.bool_param(artifacts_persisted, "artifacts_persisted"),
            step_keys_to_execute=check.list_param(
                step_keys_to_execute, "step_keys_to_execute", of_type=str
            ),
            environment_config=check.inst_param(
                environment_config, "environment_config", EnvironmentConfig
            ),
        )

    @property
    def pipeline_def(self):
        return self.pipeline.get_definition()

    def get_all_hook_defs(self):
        hook_defs = set()
        for step in self.steps:
            hook_defs = hook_defs.union(
                self.pipeline_def.get_all_hooks_for_handle(step.solid_handle)
            )
        return frozenset(hook_defs)

    def get_step_output(self, step_output_handle):
        check.inst_param(step_output_handle, "step_output_handle", StepOutputHandle)
        step = self.get_step_by_key(step_output_handle.step_key)
        return step.step_output_named(step_output_handle.output_name)

    def get_asset_store_handle(self, step_output_handle):
        check.inst_param(step_output_handle, "step_output_handle", StepOutputHandle)
        step_output = self.get_step_output(step_output_handle)
        return step_output.asset_store_handle

    def get_asset_store_key(self, step_output_handle):
        check.inst_param(step_output_handle, "step_output_handle", StepOutputHandle)
        handle = self.get_asset_store_handle(step_output_handle)
        return handle.asset_store_key if handle else None

    def has_step(self, key):
        check.str_param(key, "key")
        return key in self.step_dict

    def get_step_by_key(self, key):
        check.str_param(key, "key")
        return self.step_dict[key]

    def topological_steps(self):
        return [step for step_level in self.topological_step_levels() for step in step_level]

    def topological_step_levels(self):
        return [
            [self.step_dict[step_key] for step_key in sorted(step_key_level)]
            for step_key_level in toposort(self.deps)
        ]

    def execution_step_levels(self):
        return [
            [self.step_dict[step_key] for step_key in sorted(step_key_level)]
            for step_key_level in toposort(self.execution_deps())
        ]

    def missing_steps(self):
        return [step_key for step_key in self.step_keys_to_execute if not self.has_step(step_key)]

    def execution_deps(self):
        """
        Returns:
            Dict[str, Set[str]]: Maps step keys to sets of step keys that they depend on. Includes
                only steps that are included in step_keys_to_execute.
        """
        deps = OrderedDict()

        for key in self.step_keys_to_execute:
            deps[key] = set()

        for key in self.step_keys_to_execute:
            step = self.step_dict[key]
            for step_input in step.step_inputs:
                deps[step.key].update(
                    step_input.dependency_keys.intersection(self.step_keys_to_execute)
                )
        return deps

    def build_subset_plan(self, step_keys_to_execute):
        check.list_param(step_keys_to_execute, "step_keys_to_execute", of_type=str)
        return ExecutionPlan(
            self.pipeline,
            self.step_dict,
            self.deps,
            self.artifacts_persisted,
            step_keys_to_execute,
            self.environment_config,
        )

    def construct_asset_store_context(self, step_output_handle, asset_store_handle):
        from dagster.core.storage.asset_store import AssetStoreHandle

        check.inst_param(step_output_handle, "step_output_handle", StepOutputHandle)
        check.inst_param(asset_store_handle, "asset_store_handle", AssetStoreHandle)
        step = self.get_step_by_key(step_output_handle.step_key)
        solid_def = self.pipeline_def.solid_def_named(step.solid_handle.name)
        step_output_versions = self.resolve_step_output_versions()
        version = (
            step_output_versions[step_output_handle]
            if step_output_handle in step_output_versions
            else None
        )

        return AssetStoreContext(
            step_key=step_output_handle.step_key,
            output_name=step_output_handle.output_name,
            asset_metadata=asset_store_handle.asset_metadata,
            pipeline_name=self.pipeline_def.name,
            solid_def=solid_def,
            source_run_id=None,
            version=version,
        )

    def resolve_step_versions(self):
        return resolve_step_versions_helper(self)

    def resolve_step_output_versions(self):
        return resolve_step_output_versions_helper(self)

    def start(
        self, retries, sort_key_fn=None,
    ):
        from .active import ActiveExecution

        return ActiveExecution(self, retries, sort_key_fn)

    def step_key_for_single_step_plans(self):
        # Temporary hack to isolate single-step plans, which are often the representation of
        # sub-plans in a multiprocessing execution environment.  We want to attribute pipeline
        # events (like resource initialization) that are associated with the execution of these
        # single step sub-plans.  Most likely will be removed with the refactor detailed in
        # https://github.com/dagster-io/dagster/issues/2239
        return self.step_keys_to_execute[0] if len(self.step_keys_to_execute) == 1 else None

    @staticmethod
    def build(pipeline, environment_config, mode=None, step_keys_to_execute=None):
        """Here we build a new ExecutionPlan from a pipeline definition and the environment config.

        To do this, we iterate through the pipeline's solids in topological order, and hand off the
        execution steps for each solid to a companion _PlanBuilder object.

        Once we've processed the entire pipeline, we invoke _PlanBuilder.build() to construct the
        ExecutionPlan object.
        """
        check.inst_param(pipeline, "pipeline", IPipeline)
        check.inst_param(environment_config, "environment_config", EnvironmentConfig)
        check.opt_str_param(mode, "mode")
        check.opt_list_param(step_keys_to_execute, "step_keys_to_execute", of_type=str)

        plan_builder = _PlanBuilder(
            pipeline, environment_config, mode=mode, step_keys_to_execute=step_keys_to_execute,
        )

        # Finally, we build and return the execution plan
        return plan_builder.build()


def check_asset_store_intermediate_storage(mode_def, environment_config):
    """Only one of asset_store and intermediate_storage should be set."""
    # pylint: disable=comparison-with-callable
    from dagster.core.storage.system_storage import mem_intermediate_storage
    from dagster.core.storage.asset_store import mem_asset_store

    intermediate_storage_def = environment_config.intermediate_storage_def_for_mode(mode_def)
    intermediate_storage_is_default = (
        intermediate_storage_def is None or intermediate_storage_def == mem_intermediate_storage
    )

    asset_store = mode_def.resource_defs["asset_store"]
    asset_store_is_default = asset_store == mem_asset_store

    if not intermediate_storage_is_default and not asset_store_is_default:
        raise DagsterInvariantViolationError(
            'You have specified an intermediate storage, "{intermediate_storage_name}", and have '
            "also specified a default asset store. You must specify only one. To avoid specifying "
            "an intermediate storage, omit the intermediate_storage_defs argument to your"
            'ModeDefinition and omit "intermediate_storage" in your run config. To avoid '
            'specifying a default asset store, omit the "asset_store" key from the '
            "resource_defs argument to your ModeDefinition.".format(
                intermediate_storage_name=intermediate_storage_def.name
            )
        )
