from collections import namedtuple

from dagster.config import Field, Selector
from dagster.config.config_type import ALL_CONFIG_BUILTINS, Array, ConfigType
from dagster.config.field import check_opt_field_param
from dagster.config.field_utils import FIELD_NO_DEFAULT_PROVIDED, Shape, all_optional_type
from dagster.config.iterate_types import iterate_config_types
from dagster.core.errors import DagsterInvalidDefinitionError
from dagster.core.storage.system_storage import default_intermediate_storage_defs
from dagster.core.types.dagster_type import ALL_RUNTIME_BUILTINS, construct_dagster_type_dictionary
from dagster.utils import check, ensure_single_item

from .configurable import ConfigurableDefinition
from .definition_config_schema import IDefinitionConfigSchema
from .dependency import DependencyStructure, Solid, SolidHandle, SolidInputHandle
from .graph import GraphDefinition
from .logger import LoggerDefinition
from .mode import ModeDefinition
from .resource import ResourceDefinition
from .solid import NodeDefinition, SolidDefinition


def _is_selector_field_optional(config_type):
    check.inst_param(config_type, "config_type", ConfigType)
    if len(config_type.fields) > 1:
        return False
    else:
        _name, field = ensure_single_item(config_type.fields)
        return not field.is_required


def define_resource_dictionary_cls(resource_defs):
    check.dict_param(resource_defs, "resource_defs", key_type=str, value_type=ResourceDefinition)

    fields = {}
    for resource_name, resource_def in resource_defs.items():
        if resource_def.config_schema:
            fields[resource_name] = def_config_field(resource_def)

    return Shape(fields=fields)


def remove_none_entries(ddict):
    return {k: v for k, v in ddict.items() if v is not None}


def define_solid_config_cls(config_schema, inputs_field, outputs_field):
    check_opt_field_param(config_schema, "config_schema")
    check_opt_field_param(inputs_field, "inputs_field")
    check_opt_field_param(outputs_field, "outputs_field")

    return Shape(
        remove_none_entries(
            {"config": config_schema, "inputs": inputs_field, "outputs": outputs_field}
        ),
    )


def def_config_field(configurable_def, is_required=None):
    check.inst_param(configurable_def, "configurable_def", ConfigurableDefinition)
    return Field(
        Shape(
            {"config": configurable_def.config_field} if configurable_def.has_config_field else {}
        ),
        is_required=is_required,
    )


class EnvironmentClassCreationData(
    namedtuple(
        "EnvironmentClassCreationData",
        "pipeline_name solids dependency_structure mode_definition logger_defs ignored_solids",
    )
):
    def __new__(
        cls,
        pipeline_name,
        solids,
        dependency_structure,
        mode_definition,
        logger_defs,
        ignored_solids,
    ):
        return super(EnvironmentClassCreationData, cls).__new__(
            cls,
            pipeline_name=check.str_param(pipeline_name, "pipeline_name"),
            solids=check.list_param(solids, "solids", of_type=Solid),
            dependency_structure=check.inst_param(
                dependency_structure, "dependency_structure", DependencyStructure
            ),
            mode_definition=check.inst_param(mode_definition, "mode_definition", ModeDefinition),
            logger_defs=check.dict_param(
                logger_defs, "logger_defs", key_type=str, value_type=LoggerDefinition
            ),
            ignored_solids=check.list_param(ignored_solids, "ignored_solids", of_type=Solid),
        )


def define_logger_dictionary_cls(creation_data):
    check.inst_param(creation_data, "creation_data", EnvironmentClassCreationData)

    return Shape(
        {
            logger_name: def_config_field(logger_definition, is_required=False)
            for logger_name, logger_definition in creation_data.logger_defs.items()
        }
    )


def define_storage_field(storage_selector, storage_names, defaults):
    """Define storage field using default options, if additional storage options have been provided."""
    # If no custom storage options have been provided,
    # then users do not need to provide any configuration.
    if set(storage_names) == defaults:
        return Field(storage_selector, is_required=False)
    else:
        default_storage = FIELD_NO_DEFAULT_PROVIDED
        if len(storage_names) > 0:
            def_key = list(storage_names)[0]
            possible_default = storage_selector.fields[def_key]
            if all_optional_type(possible_default.config_type):
                default_storage = {def_key: {}}
        return Field(storage_selector, default_value=default_storage)


def define_environment_cls(creation_data):
    check.inst_param(creation_data, "creation_data", EnvironmentClassCreationData)

    intermediate_storage_field = define_storage_field(
        selector_for_named_defs(creation_data.mode_definition.intermediate_storage_defs),
        storage_names=[dfn.name for dfn in creation_data.mode_definition.intermediate_storage_defs],
        defaults=set([storage.name for storage in default_intermediate_storage_defs]),
    )
    # TODO: remove "storage" entry in run_config as part of system storage removal
    # currently we treat "storage" as an alias to "intermediate_storage" and storage field is optional
    # tracking https://github.com/dagster-io/dagster/issues/3280
    storage_field = Field(
        selector_for_named_defs(creation_data.mode_definition.intermediate_storage_defs),
        is_required=False,
    )

    return Shape(
        fields=remove_none_entries(
            {
                "solids": Field(
                    define_solid_dictionary_cls(
                        solids=creation_data.solids,
                        ignored_solids=creation_data.ignored_solids,
                        dependency_structure=creation_data.dependency_structure,
                    )
                ),
                "storage": storage_field,
                "intermediate_storage": intermediate_storage_field,
                "execution": Field(
                    selector_for_named_defs(creation_data.mode_definition.executor_defs),
                    is_required=False,
                ),
                "loggers": Field(define_logger_dictionary_cls(creation_data)),
                "resources": Field(
                    define_resource_dictionary_cls(creation_data.mode_definition.resource_defs)
                ),
            }
        ),
    )


# Common pattern for a set of named definitions (e.g. executors, intermediate storage)
# to build a selector so that one of them is selected
def selector_for_named_defs(named_defs):
    return Selector({named_def.name: def_config_field(named_def) for named_def in named_defs})


def get_inputs_field(solid, handle, dependency_structure):
    check.inst_param(solid, "solid", Solid)
    check.inst_param(handle, "handle", SolidHandle)
    check.inst_param(dependency_structure, "dependency_structure", DependencyStructure)

    if not solid.definition.has_configurable_inputs:
        return None

    inputs_field_fields = {}
    for name, inp in solid.definition.input_dict.items():
        if inp.dagster_type.loader:
            inp_handle = SolidInputHandle(solid, inp)
            # If this input is not satisfied by a dependency you must
            # provide it via config
            if not dependency_structure.has_deps(inp_handle) and not solid.container_maps_input(
                name
            ):

                inputs_field_fields[name] = Field(
                    inp.dagster_type.loader.schema_type,
                    is_required=(not solid.definition.input_has_default(name)),
                )

    if not inputs_field_fields:
        return None

    return Field(Shape(inputs_field_fields))


def get_outputs_field(solid, handle):
    check.inst_param(solid, "solid", Solid)
    check.inst_param(handle, "handle", SolidHandle)

    solid_def = solid.definition

    if not solid_def.has_configurable_outputs:
        return None

    output_dict_fields = {}
    for name, out in solid_def.output_dict.items():
        if out.dagster_type.materializer:
            output_dict_fields[name] = Field(
                out.dagster_type.materializer.schema_type, is_required=False
            )

    output_entry_dict = Shape(output_dict_fields)

    return Field(Array(output_entry_dict), is_required=False)


def solid_config_field(fields, ignored):
    if ignored:
        return Field(
            Shape(remove_none_entries(fields)),
            is_required=False,
            description="This solid is not present in the current solid selection, "
            "the config values are allowed but ignored.",
        )
    else:
        return Field(Shape(remove_none_entries(fields)))


def construct_leaf_solid_config(solid, handle, dependency_structure, config_schema, ignored):

    check.inst_param(solid, "solid", Solid)
    check.inst_param(handle, "handle", SolidHandle)
    check.inst_param(dependency_structure, "dependency_structure", DependencyStructure)
    check.opt_inst_param(config_schema, "config_schema", IDefinitionConfigSchema)
    check.bool_param(ignored, "ignored")

    return solid_config_field(
        {
            "inputs": get_inputs_field(solid, handle, dependency_structure),
            "outputs": get_outputs_field(solid, handle),
            "config": config_schema.as_field() if config_schema else None,
        },
        ignored=ignored,
    )


def define_isolid_field(solid, handle, dependency_structure, ignored):
    check.inst_param(solid, "solid", Solid)
    check.inst_param(handle, "handle", SolidHandle)

    # All solids regardless of compositing status get the same inputs and outputs
    # config. The only thing the varies is on extra element of configuration
    # 1) Vanilla solid definition: a 'config' key with the config_schema as the value
    # 2) Composite with field mapping: a 'config' key with the config_schema of
    #    the config mapping (via CompositeSolidDefinition#config_schema)
    # 3) Composite without field mapping: a 'solids' key with recursively defined
    #    solids dictionary
    # 4) `configured` composite with field mapping: a 'config' key with the config_schema that was
    #    provided when `configured` was called (via CompositeSolidDefinition#config_schema)

    if isinstance(solid.definition, SolidDefinition):
        return construct_leaf_solid_config(
            solid, handle, dependency_structure, solid.definition.config_schema, ignored,
        )

    graph_def = check.inst(solid.definition, GraphDefinition)

    if graph_def.has_config_mapping:
        # has_config_mapping covers cases 2 & 4 from above (only config mapped composite solids can
        # be `configured`)...
        return construct_leaf_solid_config(
            solid,
            handle,
            dependency_structure,
            # ...and in both cases, the correct schema for 'config' key is exposed by this property:
            graph_def.config_schema,
            ignored,
        )
        # This case omits a 'solids' key, thus if a composite solid is `configured` or has a field
        # mapping, the user cannot stub any config, inputs, or outputs for inner (child) solids.
    else:
        return solid_config_field(
            {
                "inputs": get_inputs_field(solid, handle, dependency_structure),
                "outputs": get_outputs_field(solid, handle),
                "solids": Field(
                    define_solid_dictionary_cls(
                        solids=graph_def.solids,
                        ignored_solids=None,
                        dependency_structure=graph_def.dependency_structure,
                        parent_handle=handle,
                    )
                ),
            },
            ignored=ignored,
        )


def define_solid_dictionary_cls(
    solids, ignored_solids, dependency_structure, parent_handle=None,
):
    check.list_param(solids, "solids", of_type=Solid)
    ignored_solids = check.opt_list_param(ignored_solids, "ignored_solids", of_type=Solid)
    check.inst_param(dependency_structure, "dependency_structure", DependencyStructure)
    check.opt_inst_param(parent_handle, "parent_handle", SolidHandle)

    fields = {}
    for solid in solids:
        if solid.definition.has_config_entry:
            fields[solid.name] = define_isolid_field(
                solid, SolidHandle(solid.name, parent_handle), dependency_structure, ignored=False
            )

    for solid in ignored_solids:
        if solid.definition.has_config_entry:
            fields[solid.name] = define_isolid_field(
                solid, SolidHandle(solid.name, parent_handle), dependency_structure, ignored=True
            )

    return Shape(fields)


def iterate_node_def_config_types(node_def):
    check.inst_param(node_def, "node_def", NodeDefinition)

    if isinstance(node_def, SolidDefinition):
        if node_def.has_config_field:
            yield from iterate_config_types(node_def.config_field.config_type)
    elif isinstance(node_def, GraphDefinition):
        for solid in node_def.solids:
            yield from iterate_node_def_config_types(solid.definition)

    else:
        check.invariant("Unexpected NodeDefinition type {type}".format(type=type(node_def)))


def _gather_all_schemas(node_defs):
    dagster_types = construct_dagster_type_dictionary(node_defs)
    for dagster_type in list(dagster_types.values()) + list(ALL_RUNTIME_BUILTINS):
        if dagster_type.loader:
            yield from iterate_config_types(dagster_type.loader.schema_type)
        if dagster_type.materializer:
            yield from iterate_config_types(dagster_type.materializer.schema_type)


def _gather_all_config_types(node_defs, environment_type):
    check.list_param(node_defs, "node_defs", NodeDefinition)
    check.inst_param(environment_type, "environment_type", ConfigType)

    for node_def in node_defs:
        yield from iterate_node_def_config_types(node_def)

    yield from iterate_config_types(environment_type)


def construct_config_type_dictionary(node_defs, environment_type):
    check.list_param(node_defs, "node_defs", NodeDefinition)
    check.inst_param(environment_type, "environment_type", ConfigType)

    type_dict_by_name = {t.given_name: t for t in ALL_CONFIG_BUILTINS if t.given_name}
    type_dict_by_key = {t.key: t for t in ALL_CONFIG_BUILTINS}
    all_types = list(_gather_all_config_types(node_defs, environment_type)) + list(
        _gather_all_schemas(node_defs)
    )

    for config_type in all_types:
        name = config_type.given_name
        if name and name in type_dict_by_name:
            if type(config_type) is not type(type_dict_by_name[name]):
                raise DagsterInvalidDefinitionError(
                    (
                        "Type names must be unique. You have constructed two different "
                        'instances of types with the same name "{name}".'
                    ).format(name=name)
                )
        elif name:
            type_dict_by_name[name] = config_type

        type_dict_by_key[config_type.key] = config_type

    return type_dict_by_name, type_dict_by_key
