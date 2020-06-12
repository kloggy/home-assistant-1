"""Support for scripts."""
import asyncio
import logging
from typing import List

import voluptuous as vol

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_NAME,
    CONF_ALIAS,
    CONF_ICON,
    CONF_MODE,
    EVENT_SCRIPT_STARTED,
    SERVICE_RELOAD,
    SERVICE_TOGGLE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
)
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.config_validation import make_entity_service_schema
from homeassistant.helpers.entity import ToggleEntity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.script import (
    DEFAULT_QUEUE_MAX,
    SCRIPT_MODE_CHOICES,
    SCRIPT_MODE_LEGACY,
    SCRIPT_MODE_QUEUE,
    Script,
)
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.loader import bind_hass

_LOGGER = logging.getLogger(__name__)

DOMAIN = "script"
ATTR_BACKGROUND = "background"
ATTR_CAN_CANCEL = "can_cancel"
ATTR_LAST_ACTION = "last_action"
ATTR_LAST_TRIGGERED = "last_triggered"
ATTR_VARIABLES = "variables"

CONF_DESCRIPTION = "description"
CONF_EXAMPLE = "example"
CONF_FIELDS = "fields"
CONF_SEQUENCE = "sequence"
CONF_QUEUE_MAX = "queue_size"

ENTITY_ID_FORMAT = DOMAIN + ".{}"


def _deprecated_legacy_mode(config):
    legacy_scripts = []
    for object_id, cfg in config.items():
        mode = cfg.get(CONF_MODE)
        if mode is None:
            legacy_scripts.append(object_id)
            cfg[CONF_MODE] = SCRIPT_MODE_LEGACY
    if legacy_scripts:
        _LOGGER.warning(
            "Script behavior has changed. "
            "To continue using previous behavior, which is now deprecated, "
            "add '%s: %s' to script(s): %s.",
            CONF_MODE,
            SCRIPT_MODE_LEGACY,
            ", ".join(legacy_scripts),
        )
    return config


def _queue_max(config):
    for object_id, cfg in config.items():
        mode = cfg[CONF_MODE]
        queue_max = cfg.get(CONF_QUEUE_MAX)
        if mode == SCRIPT_MODE_QUEUE:
            if queue_max is None:
                cfg[CONF_QUEUE_MAX] = DEFAULT_QUEUE_MAX
        elif queue_max is not None:
            raise vol.Invalid(
                f"{CONF_QUEUE_MAX} not valid with {mode} {CONF_MODE} "
                f"for script '{object_id}'"
            )
    return config


SCRIPT_ENTRY_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ALIAS): cv.string,
        vol.Optional(CONF_ICON): cv.icon,
        vol.Required(CONF_SEQUENCE): cv.SCRIPT_SCHEMA,
        vol.Optional(CONF_DESCRIPTION, default=""): cv.string,
        vol.Optional(CONF_FIELDS, default={}): {
            cv.string: {
                vol.Optional(CONF_DESCRIPTION): cv.string,
                vol.Optional(CONF_EXAMPLE): cv.string,
            }
        },
        vol.Optional(CONF_MODE): vol.In(SCRIPT_MODE_CHOICES),
        vol.Optional(CONF_QUEUE_MAX): vol.All(vol.Coerce(int), vol.Range(min=2)),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.schema_with_slug_keys(SCRIPT_ENTRY_SCHEMA),
            _deprecated_legacy_mode,
            _queue_max,
        )
    },
    extra=vol.ALLOW_EXTRA,
)

SCRIPT_SERVICE_SCHEMA = vol.Schema(dict)
SCRIPT_TOGGLE_TURN_OFF_SCHEMA = make_entity_service_schema(
    {vol.Optional(ATTR_VARIABLES): dict}
)
SCRIPT_TURN_ON_SCHEMA = make_entity_service_schema(
    {
        vol.Optional(ATTR_VARIABLES): dict,
        vol.Optional(ATTR_BACKGROUND, default=False): cv.boolean,
    }
)
RELOAD_SERVICE_SCHEMA = vol.Schema({})


@bind_hass
def is_on(hass, entity_id):
    """Return if the script is on based on the statemachine."""
    return hass.states.is_state(entity_id, STATE_ON)


@callback
def scripts_with_entity(hass: HomeAssistant, entity_id: str) -> List[str]:
    """Return all scripts that reference the entity."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    results = []

    for script_entity in component.entities:
        if entity_id in script_entity.script.referenced_entities:
            results.append(script_entity.entity_id)

    return results


@callback
def entities_in_script(hass: HomeAssistant, entity_id: str) -> List[str]:
    """Return all entities in script."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    script_entity = component.get_entity(entity_id)

    if script_entity is None:
        return []

    return list(script_entity.script.referenced_entities)


@callback
def scripts_with_device(hass: HomeAssistant, device_id: str) -> List[str]:
    """Return all scripts that reference the device."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    results = []

    for script_entity in component.entities:
        if device_id in script_entity.script.referenced_devices:
            results.append(script_entity.entity_id)

    return results


@callback
def devices_in_script(hass: HomeAssistant, entity_id: str) -> List[str]:
    """Return all devices in script."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    script_entity = component.get_entity(entity_id)

    if script_entity is None:
        return []

    return list(script_entity.script.referenced_devices)


async def async_setup(hass, config):
    """Load the scripts from the configuration."""
    hass.data[DOMAIN] = component = EntityComponent(_LOGGER, DOMAIN, hass)

    await _async_process_config(hass, config, component)

    async def reload_service(service):
        """Call a service to reload scripts."""
        conf = await component.async_prepare_reload()
        if conf is None:
            return

        await _async_process_config(hass, conf, component)

    async def turn_on_toggle_service(service, legacy_cofunc, non_legacy_meth, **kwargs):
        script_entities = await component.async_extract_from_service(service)

        # Legacy scripts will be run as before, so split them out.
        legacy_script_entities = [
            script_entity
            for script_entity in script_entities
            if script_entity.script.is_legacy
        ]
        script_entities = [
            script_entity
            for script_entity in script_entities
            if not script_entity.script.is_legacy
        ]

        tasks = []
        # Legacy scripts will be run one after the other. Any script that causes an
        # exception will prevent the rest from running.
        if legacy_script_entities:
            tasks.append(hass.async_create_task(legacy_cofunc(legacy_script_entities)))
        # All new style scripts will be run concurrently and independently.
        for script_entity in script_entities:
            tasks.append(
                hass.async_create_task(
                    getattr(script_entity, non_legacy_meth)(
                        context=service.context, **kwargs
                    )
                )
            )
        if not tasks:
            return

        # Watch for exceptions. Capture first to re-raise when all scripts are done or
        # when we're cancelled. Log any other exceptions with traceback.
        first_exception = None
        try:
            for future in asyncio.as_completed(tasks):
                try:
                    await future
                except Exception as ex:  # pylint: disable=broad-except
                    if not first_exception:
                        first_exception = ex
                    else:
                        _LOGGER.exception("While running script: %s", ex)
        except asyncio.CancelledError:
            # Cancel any tasks that haven't completed.
            for task in tasks:
                task.cancel()
            raise
        finally:
            if first_exception:
                raise first_exception

    async def turn_on_service(service):
        """Call a service to turn script on."""
        variables = service.data.get(ATTR_VARIABLES)

        async def turn_on_legacy(legacy_script_entities):
            for script_entity in legacy_script_entities:
                await hass.services.async_call(
                    DOMAIN, script_entity.object_id, variables, context=service.context
                )

        await turn_on_toggle_service(
            service,
            turn_on_legacy,
            "async_turn_on",
            variables=variables,
            background=service.data[ATTR_BACKGROUND],
        )

    async def turn_off_service(service):
        """Cancel a script."""
        script_entities = await component.async_extract_from_service(service)

        if not script_entities:
            return

        tasks = [
            hass.async_create_task(script_entity.async_turn_off())
            for script_entity in script_entities
        ]
        try:
            done, _ = await asyncio.wait(tasks)
        except asyncio.CancelledError:
            # Cancel any tasks that haven't completed.
            for task in tasks:
                task.cancel()
            raise
        else:
            # Propagate any exceptions that might have happened.
            for done_task in done:
                done_task.result()

    async def toggle_service(service):
        """Toggle a script."""

        async def toggle_legacy(legacy_script_entities):
            for script_entity in legacy_script_entities:
                await script_entity.async_toggle(context=service.context)

        await turn_on_toggle_service(service, toggle_legacy, "async_toggle")

    hass.services.async_register(
        DOMAIN, SERVICE_RELOAD, reload_service, schema=RELOAD_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_TURN_ON, turn_on_service, schema=SCRIPT_TURN_ON_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_TURN_OFF, turn_off_service, schema=SCRIPT_TOGGLE_TURN_OFF_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_TOGGLE, toggle_service, schema=SCRIPT_TOGGLE_TURN_OFF_SCHEMA
    )

    return True


async def _async_process_config(hass, config, component):
    """Process script configuration."""

    async def service_handler(service):
        """Execute a service call to script.<script name>."""
        entity_id = ENTITY_ID_FORMAT.format(service.service)
        script_entity = component.get_entity(entity_id)
        if script_entity.script.is_legacy and script_entity.is_on:
            _LOGGER.warning("Script %s already running.", entity_id)
            return
        await script_entity.async_turn_on(
            variables=service.data, context=service.context
        )

    script_entities = []

    for object_id, cfg in config.get(DOMAIN, {}).items():
        script_entities.append(
            ScriptEntity(
                hass,
                object_id,
                cfg.get(CONF_ALIAS, object_id),
                cfg.get(CONF_ICON),
                cfg[CONF_SEQUENCE],
                cfg[CONF_MODE],
                cfg.get(CONF_QUEUE_MAX, 0),
            )
        )

    await component.async_add_entities(script_entities)

    # Register services for all entities that were created successfully.
    for script_entity in script_entities:
        object_id = script_entity.object_id
        if component.get_entity(script_entity.entity_id) is None:
            _LOGGER.error("Couldn't load script %s", object_id)
            continue

        cfg = config[DOMAIN][object_id]

        hass.services.async_register(
            DOMAIN, object_id, service_handler, schema=SCRIPT_SERVICE_SCHEMA
        )

        # Register the service description
        service_desc = {
            CONF_DESCRIPTION: cfg[CONF_DESCRIPTION],
            CONF_FIELDS: cfg[CONF_FIELDS],
        }
        async_set_service_schema(hass, DOMAIN, object_id, service_desc)


class ScriptEntity(ToggleEntity):
    """Representation of a script entity."""

    icon = None

    def __init__(self, hass, object_id, name, icon, sequence, mode, queue_max):
        """Initialize the script."""
        self.object_id = object_id
        self.icon = icon
        self.entity_id = ENTITY_ID_FORMAT.format(object_id)
        self.script = Script(
            hass,
            sequence,
            name,
            self.async_change_listener,
            mode,
            queue_max,
            logging.getLogger(f"{__name__}.{object_id}"),
        )
        self._changed = asyncio.Event()

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def name(self):
        """Return the name of the entity."""
        return self.script.name

    @property
    def state_attributes(self):
        """Return the state attributes."""
        attrs = {}
        attrs[ATTR_LAST_TRIGGERED] = self.script.last_triggered
        if self.script.can_cancel:
            attrs[ATTR_CAN_CANCEL] = self.script.can_cancel
        if self.script.last_action:
            attrs[ATTR_LAST_ACTION] = self.script.last_action
        return attrs

    @property
    def is_on(self):
        """Return true if script is on."""
        return self.script.is_running

    @callback
    def async_change_listener(self):
        """Update state."""
        self.async_write_ha_state()
        self._changed.set()

    async def async_turn_on(self, **kwargs):
        """Turn the script on."""
        background = kwargs.get("background", False)
        context = kwargs.get("context")
        self.async_set_context(context)
        self.hass.bus.async_fire(
            EVENT_SCRIPT_STARTED,
            {ATTR_NAME: self.script.name, ATTR_ENTITY_ID: self.entity_id},
            context=context,
        )
        coro = self.script.async_run(kwargs.get(ATTR_VARIABLES), context)
        if background:
            # Not a blocking call so let script run in separate Task. However, wait for
            # first state change so we can guarantee that it is written to the State
            # Machine before we return.
            self._changed.clear()
            self.hass.async_create_task(coro)
            await self._changed.wait()
        else:
            await coro

    async def async_turn_off(self, **kwargs):
        """Turn script off."""
        await self.script.async_stop()

    async def async_will_remove_from_hass(self):
        """Stop script and remove service when it will be removed from Home Assistant."""
        await self.script.async_stop()

        # remove service
        self.hass.services.async_remove(DOMAIN, self.object_id)
