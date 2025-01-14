from __future__ import annotations

import pathlib
import sys
import typing as t
from pathlib import Path

from pydantic import Field
from sqlglot import exp
from sqlglot.optimizer.qualify_columns import quote_identifiers

from sqlmesh.core import constants as c
from sqlmesh.core import dialect as d
from sqlmesh.core.model.common import bool_validator, expression_validator
from sqlmesh.core.model.definition import _Model
from sqlmesh.core.node import _Node
from sqlmesh.core.renderer import QueryRenderer
from sqlmesh.utils.date import TimeLike
from sqlmesh.utils.errors import AuditConfigError, SQLMeshError, raise_config_error
from sqlmesh.utils.hashing import hash_data
from sqlmesh.utils.jinja import JinjaMacroRegistry
from sqlmesh.utils.metaprogramming import Executable
from sqlmesh.utils.pydantic import (
    PydanticModel,
    field_validator,
    model_validator,
    model_validator_v1_args,
)

if t.TYPE_CHECKING:
    from sqlmesh.core.snapshot import Node, Snapshot

if sys.version_info >= (3, 9):
    from typing import Literal
else:
    from typing_extensions import Literal


class AuditMixin:
    """
    Mixin for common Audit functionality

    Args:
        name: The unique name of the audit.
        dialect: The dialect of the audit query.
        skip: Setting this to `true` will cause this audit to be skipped. Defaults to `false`.
        blocking: Setting this to `true` will cause the pipeline execution to stop if this audit fails.
        query: The audit query.
        defaults: Default values for the audit query.
        expressions: Additional sql statements to execute alongside the audit.
        jinja_macros: A registry of jinja macros to use when rendering the audit query.
    """

    name: str
    dialect: str
    skip: bool
    blocking: bool
    query: t.Union[exp.Subqueryable, d.JinjaQuery]
    defaults: t.Dict[str, exp.Expression]
    expressions_: t.Optional[t.List[exp.Expression]]
    jinja_macros: JinjaMacroRegistry

    _path: t.Optional[pathlib.Path]

    def render_query(
        self,
        snapshot_or_node: t.Union[Snapshot, _Node],
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        is_dev: bool = False,
        **kwargs: t.Any,
    ) -> exp.Subqueryable:
        """Renders the audit's query.

        Args:
            snapshot_or_node: The snapshot or node which is being audited.
            start: The start datetime to render. Defaults to epoch start.
            end: The end datetime to render. Defaults to epoch start.
            execution_time: The date/time time reference to use for execution time.
            snapshots: All snapshots (by name) to use for mapping of physical locations.
            audit_name: The name of audit if the query to render is for an audit.
            is_dev: Indicates whether the rendering happens in the development mode and temporary
                tables / table clones should be used where applicable.
            kwargs: Additional kwargs to pass to the renderer.

        Returns:
            The rendered expression.
        """
        node = snapshot_or_node if isinstance(snapshot_or_node, _Node) else snapshot_or_node.node
        query_renderer = self._create_query_renderer(node)

        rendered_query = query_renderer.render(
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            is_dev=is_dev,
            **{**self.defaults, **kwargs},  # type: ignore
        )

        if rendered_query is None:
            raise SQLMeshError(
                f"Failed to render query for audit '{self.name}', node '{node.name}'."
            )

        return rendered_query

    @property
    def expressions(self) -> t.List[exp.Expression]:
        return self.expressions_ or []

    @property
    def macro_definitions(self) -> t.List[d.MacroDef]:
        """All macro definitions from the list of expressions."""
        return [s for s in self.expressions if isinstance(s, d.MacroDef)]

    def _create_query_renderer(self, node: _Node) -> QueryRenderer:
        raise NotImplementedError


@field_validator("name", "dialect", mode="before", check_fields=False)
def audit_string_validator(cls: t.Type, v: t.Any) -> t.Optional[str]:
    if isinstance(v, exp.Expression):
        return v.name.lower()
    return str(v).lower() if v is not None else None


@field_validator("defaults", mode="before", check_fields=False)
def audit_map_validator(cls: t.Type, v: t.Any) -> t.Dict[str, t.Any]:
    if isinstance(v, (exp.Tuple, exp.Array)):
        return dict(map(_maybe_parse_arg_pair, v.expressions))
    elif isinstance(v, dict):
        return v
    else:
        raise_config_error(
            "Defaults must be a tuple of exp.EQ or a dict", error_type=AuditConfigError
        )
    return {}


class ModelAudit(PydanticModel, AuditMixin, frozen=True):
    """
    Audit is an assertion made about your tables.

    An audit is a SQL query that returns bad records.
    """

    name: str
    dialect: str = ""
    skip: bool = False
    blocking: bool = True
    query: t.Union[exp.Subqueryable, d.JinjaQuery]
    defaults: t.Dict[str, exp.Expression] = {}
    expressions_: t.Optional[t.List[exp.Expression]] = Field(default=None, alias="expressions")
    jinja_macros: JinjaMacroRegistry = JinjaMacroRegistry()

    _path: t.Optional[pathlib.Path] = None

    # Validators
    _query_validator = expression_validator
    _bool_validator = bool_validator
    _string_validator = audit_string_validator
    _map_validator = audit_map_validator

    def render_query(
        self,
        snapshot_or_node: t.Union[Snapshot, _Node],
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        is_dev: bool = False,
        **kwargs: t.Any,
    ) -> exp.Subqueryable:
        from sqlmesh.core.snapshot import Snapshot

        extra_kwargs = {}

        node = snapshot_or_node if isinstance(snapshot_or_node, _Node) else snapshot_or_node.node
        this_model = (
            node.name
            if isinstance(snapshot_or_node, _Node)
            else t.cast(Snapshot, snapshot_or_node).table_name(is_dev=is_dev, for_read=True)
        )

        columns_to_types: t.Optional[t.Dict[str, t.Any]] = None
        if "engine_adapter" in kwargs:
            try:
                columns_to_types = kwargs["engine_adapter"].columns(this_model)
            except Exception:
                pass

        node = t.cast(_Model, node)
        if node.time_column:
            where = exp.column(node.time_column.column).between(
                node.convert_to_time_column(start or c.EPOCH, columns_to_types),
                node.convert_to_time_column(end or c.EPOCH, columns_to_types),
            )
        else:
            where = None

        # The model's name is already normalized, but in case of snapshots we also prepend a
        # case-sensitive physical schema name, so we quote here to ensure that we won't have
        # a broken schema reference after the resulting query is normalized in `render`.
        quoted_model_name = quote_identifiers(
            exp.to_table(this_model, dialect=self.dialect), dialect=self.dialect
        )
        extra_kwargs["this_model"] = (
            exp.select("*").from_(quoted_model_name).where(where).subquery()
        )

        return super().render_query(
            snapshot_or_node,
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            is_dev=is_dev,
            **{**extra_kwargs, **kwargs},
        )

    def _create_query_renderer(self, node: _Node) -> QueryRenderer:
        model = t.cast(_Model, node)
        return QueryRenderer(
            self.query,
            self.dialect or model.dialect,
            self.macro_definitions,
            path=self._path or Path(),
            jinja_macro_registry=self.jinja_macros,
            python_env=model.python_env,
            only_execution_time=model.kind.only_execution_time,
        )

    @classmethod
    def load(
        cls,
        expressions: t.List[exp.Expression],
        *,
        path: pathlib.Path,
        dialect: t.Optional[str] = None,
    ) -> ModelAudit:
        """Load an audit from a parsed SQLMesh audit file.

        Args:
            expressions: Audit, *Statements, Query
            path: An optional path of the file.
            dialect: The default dialect if no audit dialect is configured.
        """
        if len(expressions) < 2:
            _raise_config_error("Incomplete audit definition, missing AUDIT or QUERY", path)

        meta, *statements, query = expressions

        if not isinstance(meta, d.Audit):
            _raise_config_error(
                "AUDIT statement is required as the first statement in the definition",
                path,
            )
            raise

        provided_meta_fields = {p.name for p in meta.expressions}
        provided_meta_fields.add("query")

        missing_required_fields = cls.missing_required_fields(provided_meta_fields)
        if missing_required_fields:
            breakpoint()
            _raise_config_error(
                f"Missing required fields {missing_required_fields} in the audit definition",
                path,
            )

        extra_fields = cls.extra_fields(provided_meta_fields)
        if extra_fields:
            _raise_config_error(
                f"Invalid extra fields {extra_fields} in the audit definition", path
            )

        if not isinstance(query, exp.Subqueryable):
            _raise_config_error("Missing SELECT query in the audit definition", path)
            raise

        try:
            audit = cls(
                query=query,
                expressions=statements,
                **{
                    "dialect": dialect or "",
                    **{prop.name: prop.args.get("value") for prop in meta.expressions if prop},
                },
            )
        except Exception as ex:
            _raise_config_error(str(ex), path)

        audit._path = path
        return audit

    @classmethod
    def load_multiple(
        cls,
        expressions: t.List[exp.Expression],
        *,
        path: pathlib.Path,
        dialect: t.Optional[str] = None,
    ) -> t.Generator[ModelAudit, None, None]:
        audit_block: t.List[exp.Expression] = []
        for expression in expressions:
            if isinstance(expression, d.Audit):
                if audit_block:
                    yield cls.load(
                        expressions=audit_block,
                        path=path,
                        dialect=dialect,
                    )
                    audit_block.clear()
            audit_block.append(expression)
        yield cls.load(
            expressions=audit_block,
            path=path,
            dialect=dialect,
        )


class StandaloneAudit(_Node, AuditMixin):
    """
    Args:
        depends_on: A list of tables this audit depends on.
        hash_raw_query: Whether to hash the raw query or the rendered query.
        python_env: Dictionary containing all global variables needed to render the audit's macros.
    """

    name: str
    dialect: str = ""
    skip: bool = False
    blocking: bool = False
    query: t.Union[exp.Subqueryable, d.JinjaQuery]
    defaults: t.Dict[str, exp.Expression] = {}
    expressions_: t.Optional[t.List[exp.Expression]] = Field(default=None, alias="expressions")
    jinja_macros: JinjaMacroRegistry = JinjaMacroRegistry()
    depends_on_: t.Optional[t.Set[str]] = Field(default=None, alias="depends_on")
    hash_raw_query: bool = False
    python_env_: t.Optional[t.Dict[str, Executable]] = Field(default=None, alias="python_env")

    source_type: Literal["audit"] = "audit"

    _path: t.Optional[pathlib.Path] = None
    _depends_on: t.Optional[t.Set[str]] = None

    # Validators
    _query_validator = expression_validator
    _bool_validator = bool_validator
    _string_validator = audit_string_validator
    _map_validator = audit_map_validator

    @model_validator(mode="after")
    @model_validator_v1_args
    def _node_root_validator(cls, values: t.Dict[str, t.Any]) -> t.Dict[str, t.Any]:
        if values.get("blocking"):
            name = values.get("name")
            raise AuditConfigError(f"Standalone audits cannot be blocking: '{name}'.")
        return values

    @property
    def depends_on(self) -> t.Set[str]:
        if self._depends_on is None:
            self._depends_on = self.depends_on_ or set()

            query = self.render_query(self)
            if query is not None:
                self._depends_on |= d.find_tables(query, dialect=self.dialect)

            self._depends_on -= {self.name}
        return self._depends_on

    @property
    def python_env(self) -> t.Dict[str, Executable]:
        return self.python_env_ or {}

    @property
    def sorted_python_env(self) -> t.List[t.Tuple[str, Executable]]:
        """Returns the python env sorted by executable kind and then var name."""
        return sorted(self.python_env.items(), key=lambda x: (x[1].kind, x[0]))

    @property
    def data_hash(self) -> str:
        """
        Computes the data hash for the node.

        Returns:
            The data hash for the node.
        """
        # StandaloneAudits do not have a data hash
        return hash_data("")

    def metadata_hash(self, audits: t.Dict[str, ModelAudit]) -> str:
        """
        Computes the metadata hash for the node.

        Args:
            audits: Available audits by name.

        Returns:
            The metadata hash for the node.
        """
        data = [
            self.owner,
            self.description,
            *sorted(self.tags),
            str(self.sorted_python_env),
            self.stamp,
        ]

        query = self.query if self.hash_raw_query else self.render_query(self) or self.query
        data.append(query.sql(comments=False))

        return hash_data(data)

    def text_diff(self, other: Node) -> str:
        """Produce a text diff against another node.

        Args:
            other: The node to diff against.

        Returns:
            A unified text diff showing additions and deletions.
        """
        if not isinstance(other, StandaloneAudit):
            raise SQLMeshError(
                f"Cannot diff audit '{self.name} against a non-audit node '{other.name}'"
            )

        return d.text_diff(self.query, other.query, self.dialect)

    @property
    def is_audit(self) -> bool:
        """Return True if this is an audit node"""
        return True

    def _create_query_renderer(self, node: _Node) -> QueryRenderer:
        audit = t.cast(StandaloneAudit, node)
        return QueryRenderer(
            self.query,
            self.dialect,
            self.macro_definitions,
            path=self._path or Path(),
            jinja_macro_registry=self.jinja_macros,
            python_env=audit.python_env,
        )


Audit = t.Union[ModelAudit, StandaloneAudit]


class AuditResult(PydanticModel):
    audit: Audit
    """The audit this result is for."""
    model: t.Optional[_Model] = None
    """The model this audit is for."""
    count: t.Optional[int] = None
    """The number of records returned by the audit query. This could be None if the audit was skipped."""
    query: t.Optional[exp.Expression] = None
    """The rendered query used by the audit. This could be None if the audit was skipped."""
    skipped: bool = False
    """Whether this audit was skipped or not."""


def _raise_config_error(msg: str, path: pathlib.Path) -> None:
    raise_config_error(msg, location=path, error_type=AuditConfigError)


# mypy doesn't realize raise_config_error raises an exception
@t.no_type_check
def _maybe_parse_arg_pair(e: exp.Expression) -> t.Tuple[str, exp.Expression]:
    if isinstance(e, exp.EQ):
        return e.left.name, e.right
    raise_config_error(f"Invalid defaults expression: {e}", error_type=AuditConfigError)
