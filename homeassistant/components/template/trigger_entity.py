"""Trigger entity."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from typing import Any

from homeassistant.const import (
    CONF_STATE,
    CONF_VARIABLES,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.script_variables import ScriptVariables
from homeassistant.helpers.template import _SENTINEL, Template, TemplateError
from homeassistant.helpers.trigger_template_entity import (
    TriggerBaseEntity,
    log_triggered_template_error,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TriggerUpdateCoordinator
from .entity import AbstractTemplateEntity


class TriggerStateTemplate:
    """State template linked to template trigger entity."""

    def __init__(
        self,
        entity: AbstractTemplateEntity,
        attribute: str,
        template: Template,
        on_render: Callable[[Any], Any] | None = None,
        on_update: Callable[[Any], None] | None = None,
        on_cancel: Callable[[Any], None] | None = None,
    ) -> None:
        """Trigger State Template."""
        self._entity = entity
        self.attribute = attribute
        self._template = template
        self._on_render = on_render
        self._on_update = on_update
        self._on_cancel = on_cancel
        self.result: Any | None = None

    def async_render(self, variables: ConfigType) -> bool:
        """Render the state tempalte."""
        result = _SENTINEL
        try:
            result = self._template.async_render(variables, parse_result=True)
        except TemplateError as err:
            log_triggered_template_error(self._entity.entity_id, err, CONF_STATE)

        if result is _SENTINEL:
            return False

        self.result = self._on_render(result) if self._on_render else result
        return True

    @callback
    def on_cancel(self) -> None:
        """Cancel anything that needs to be canceled."""
        if self._on_cancel:
            self._on_cancel(self.result)

    @callback
    def on_update(self) -> None:
        """Cancel anything that needs to be canceled."""
        if self._on_update:
            self._on_update(self.result)
        else:
            setattr(self._entity, self.attribute, self.result)


class TriggerEntity(  # pylint: disable=hass-enforce-class-module
    TriggerBaseEntity,
    CoordinatorEntity[TriggerUpdateCoordinator],
    AbstractTemplateEntity,
):
    """Template entity based on trigger data."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: TriggerUpdateCoordinator,
        config: dict,
    ) -> None:
        """Initialize the entity."""
        CoordinatorEntity.__init__(self, coordinator)
        TriggerBaseEntity.__init__(self, hass, config)
        AbstractTemplateEntity.__init__(self, hass, config)

        self._entity_variables: ScriptVariables | None = config.get(CONF_VARIABLES)
        self._rendered_entity_variables: dict | None = None
        self._state_rendered_unavailable = False
        self._state_template: TriggerStateTemplate | None = None

    async def async_added_to_hass(self) -> None:
        """Handle being added to Home Assistant."""
        await super().async_added_to_hass()
        if self.coordinator.data is not None:
            self._process_data()

    def _set_unique_id(self, unique_id: str | None) -> None:
        """Set unique id."""
        if unique_id and self.coordinator.unique_id:
            self._unique_id = f"{self.coordinator.unique_id}-{unique_id}"
        else:
            self._unique_id = unique_id

    @property
    def referenced_blueprint(self) -> str | None:
        """Return referenced blueprint or None."""
        return self.coordinator.referenced_blueprint

    @property
    def available(self) -> bool:
        """Return availability of the entity."""
        if self._state_rendered_unavailable:
            return False

        return super().available

    def setup_state_template(
        self,
        attribute: str,
        on_render: Callable[[Any], Any] | None = None,
        on_update: Callable[[Any], None] | None = None,
        on_unknown_or_unavailable: Callable[[None], None] | None = None,
    ) -> TriggerStateTemplate:
        """Setup the template that manages the entity state."""
        template: Template | None = self._config.get(CONF_STATE)
        if isinstance(template, Template):
            self._state_template = TriggerStateTemplate(
                attribute, template, on_render, on_update, on_unknown_or_unavailable
            )

    @callback
    @abstractmethod
    def _process_rendered_data(self) -> bool:
        """Process additional templates.

        Returns True when any updates are made.
        """
        return False

    @callback
    def _render_script_variables(self) -> dict:
        """Render configured variables."""
        return self._rendered_entity_variables or {}

    def _render_state_template(self, variables: ConfigType) -> bool:
        """Check the state for unavailable."""
        self._state_rendered_unavailable = False
        if not (state := self._state_template):
            return False

        # If state fails to render, the entity should go unavailable. Render the
        # state as a simple template because the result should always be a string or None.
        if not state.async_render(variables):
            state.on_cancel()
            self._state_rendered_unavailable = True
            return True

        if (result := state.result) is None:
            # Unknown, set the state to None and render remaining templates.
            setattr(self, state.attribute, None)
            state.on_cancel()
            self._render_templates(variables)
        elif isinstance(result, str):
            if (state_str := result.lower()) == STATE_UNAVAILABLE:
                # Unavailable, cancel any callbacks
                state.on_cancel()
                self._state_rendered_unavailable = True
            elif state_str == STATE_UNKNOWN:
                # Unknown, set the state to None and render remaining templates.
                setattr(self, state.attribute, None)
                state.on_cancel()
                self._render_templates(variables)
            else:
                # Known State
                self._render_templates(variables)
                state.on_update()
        else:
            # Known State
            self._render_templates(variables)
            state.on_update()

        return True

    def _render_templates(self, variables: ConfigType) -> None:
        """Render templates."""
        rendered = dict(self._static_rendered)
        self._render_single_templates(rendered, variables)
        self._render_attributes(rendered, variables)
        self._rendered = rendered

    @callback
    def _process_data(self) -> bool:
        """Process new data."""

        coordinator_variables = self.coordinator.data["run_variables"]
        if self._entity_variables:
            entity_variables = self._entity_variables.async_simple_render(
                coordinator_variables
            )
            self._rendered_entity_variables = {
                **coordinator_variables,
                **entity_variables,
            }
        else:
            self._rendered_entity_variables = coordinator_variables
        variables = self._template_variables(self._rendered_entity_variables)

        write_ha_state = False
        if self._render_availability_template(variables):
            if self._attr_assumed_state:
                self._render_templates(variables)
                # Ensure we update with an optimistic entity
                # if have any changes in icon, picture, or name
                write_ha_state = len(self._rendered) > 0
            else:
                # Render the state, only render additional templates
                # if the state renders available.
                write_ha_state = self._render_state_template(variables)
                if not self._state_rendered_unavailable:
                    # Process the remaining rendered template data
                    write_ha_state = self._process_rendered_data() or write_ha_state
        elif state := self._state_template:
            # Unavailable, cancel any callbacks
            state.on_cancel()
            write_ha_state = True

        if write_ha_state:
            self.async_set_context(self.coordinator.data["context"])

        return write_ha_state

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._process_data():
            self.async_write_ha_state()
