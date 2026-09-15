from enum import auto

from gwalerter.sema.enums.gw_str_enum import SemaEnum


class SystemMode(SemaEnum):
    """Sema: https://schemas.electricity.works/enums/gw1.system.mode/000"""

    Heating = auto()
    Standby = auto()
    MonitorOnly = auto()

    @classmethod
    def default(cls) -> "SystemMode":
        return cls.Heating

    @classmethod
    def values(cls) -> list[str]:
        return [elt.value for elt in cls]

    @classmethod
    def enum_name(cls) -> str:
        return "gw1.system.mode"

    @classmethod
    def enum_version(cls) -> str:
        return "000"
