"""Template entity base class."""

from abc import abstractmethod
from collections.abc import Callable, Sequence
from enum import StrEnum
import logging
from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_DEVICE_ID, CONF_OPTIMISTIC, CONF_STATE
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.entity import Entity, async_generate_entity_id
from homeassistant.helpers.script import Script, _VarsType
from homeassistant.helpers.template import TemplateStateFromEntityId
from homeassistant.helpers.typing import ConfigType

from .const import CONF_DEFAULT_ENTITY_ID, RESULT_OFF, RESULT_ON

_LOGGER = logging.getLogger(__name__)


class AbstractTemplateEntity(Entity):
    """Actions linked to a template entity."""

    _entity_id_format: str
    _optimistic_entity: bool = False
    _extra_optimistic_options: tuple[str, ...] | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        config: ConfigType,
    ) -> None:
        """Initialize the entity."""

        self.hass = hass
        self._action_scripts: dict[str, Script] = {}

        if self._optimistic_entity:
            optimistic = config.get(CONF_OPTIMISTIC)

            assumed_optimistic = config.get(CONF_STATE) is None
            if self._extra_optimistic_options:
                assumed_optimistic = assumed_optimistic and all(
                    config.get(option) is None
                    for option in self._extra_optimistic_options
                )

            self._attr_assumed_state = optimistic or (
                optimistic is None and assumed_optimistic
            )

        if (default_entity_id := config.get(CONF_DEFAULT_ENTITY_ID)) is not None:
            _, _, object_id = default_entity_id.partition(".")
            self.entity_id = async_generate_entity_id(
                self._entity_id_format, object_id, hass=self.hass
            )

        device_registry = dr.async_get(hass)
        if (device_id := config.get(CONF_DEVICE_ID)) is not None:
            self.device_entry = device_registry.async_get(device_id)

    @property
    @abstractmethod
    def referenced_blueprint(self) -> str | None:
        """Return referenced blueprint or None."""

    @callback
    @abstractmethod
    def _render_script_variables(self) -> dict:
        """Render configured variables."""

    def add_script(
        self,
        script_id: str,
        config: Sequence[dict[str, Any]],
        name: str,
        domain: str,
    ):
        """Add an action script."""

        self._action_scripts[script_id] = Script(
            self.hass,
            config,
            f"{name} {script_id}",
            domain,
        )

    async def async_run_script(
        self,
        script: Script,
        *,
        run_variables: _VarsType | None = None,
        context: Context | None = None,
    ) -> None:
        """Run an action script."""
        if run_variables is None:
            run_variables = {}
        await script.async_run(
            run_variables={
                "this": TemplateStateFromEntityId(self.hass, self.entity_id),
                **self._render_script_variables(),
                **run_variables,
            },
            context=context,
        )


def log_result_error(
    domain: str, entity_id: str, attribute: str, value: Any, expected: tuple[str]
) -> None:
    """Log a template result error."""
    _LOGGER.error(
        "Recieved invalid %s %s value '%s' for entity %s, expected: %s",
        domain,
        attribute,
        value,
        entity_id,
        ", ".join(expected),
    )


class TemplateResultHandler:
    """Class for converting template results."""

    def __init__(self, entity: AbstractTemplateEntity, domain: str) -> None:
        """Initialize the converter."""
        self._entity = entity
        self._domain = domain

    def as_enum[T: StrEnum](
        self,
        attribute: str,
        state_enum: type[T],
    ) -> Callable[[Any], T | None]:
        """Converts the template result to an StrEnum.

        All strings will attempt to convert to the StrEnum
        Anything that cannot convert will result in None.
        """

        def convert(result: Any) -> T:
            if isinstance(result, str):
                value = result.lower().strip()
                try:
                    return state_enum(value)
                except ValueError:
                    pass

            log_result_error(
                self._domain,
                self._entity.entity_id,
                attribute,
                result,
                tuple(s.value for s in state_enum),
            )
            return None

        return convert

    def as_enum_with_on_off[T: StrEnum](
        self,
        attribute: str,
        state_enum: type[T],
        on_state: T,
        off_state: T,
    ) -> Callable[[Any], T | None]:
        """Converts the template result to an StrEnum.

        Boolean results will be converted to `on_state` and `off_state`
        All strings will attempt to convert to the StrEnum
        Anything that cannot convert will result in None.
        """

        def convert(result: Any) -> T:
            if isinstance(result, bool):
                return on_state if result else off_state

            if isinstance(result, str):
                value = result.lower().strip()
                try:
                    return state_enum(value)
                except ValueError:
                    try:
                        return on_state if cv.boolean(value) else off_state
                    except vol.Invalid:
                        pass

            log_result_error(
                self._domain,
                self._entity.entity_id,
                attribute,
                result,
                RESULT_ON + RESULT_OFF + tuple(s.value for s in state_enum),
            )
            return None

        return convert

    def as_boolean(
        self,
        attribute: str,
        as_true: tuple[str] | None = None,
        as_false: tuple[str] | None = None,
    ) -> Callable[[Any], bool | None]:
        """Convert the result to a boolean.

        True/not 0/'1'/'true'/'yes'/'on'/'enable' are considered truthy
        False/0/None/'0'/'false'/'no'/'off'/'disable' are considered falsy
        Additional values provided by as_true are considered truthy
        Additional values provided by as_false are considered truthy
        All other values are None
        """

        def convert(result: Any) -> bool | None:
            if result is None:
                return False
            if isinstance(result, bool):
                return result
            if isinstance(result, str) and (as_true or as_false):
                value = result.lower().strip()
                if as_true and value in as_true:
                    return True
                if as_false and value in as_false:
                    return False

            try:
                return cv.boolean(result)
            except vol.Invalid:
                log_result_error(
                    self._domain,
                    self._entity.entity_id,
                    attribute,
                    result,
                    RESULT_ON + RESULT_OFF,
                )
                return None

        return convert
