from enum import auto

from gwalerter.sema.enums.gw_str_enum import SemaEnum


class AlertCategory(SemaEnum):
    """Sema: https://schemas.electricity.works/enums/gw.alert.category/000"""

    Unknown = auto()
    House = auto()
    Fleet = auto()
    PlatformService = auto()

    @classmethod
    def default(cls) -> "AlertCategory":
        return cls.Unknown

    @classmethod
    def values(cls) -> list[str]:
        return [elt.value for elt in cls]

    @classmethod
    def enum_name(cls) -> str:
        return "gw.alert.category"

    @classmethod
    def enum_version(cls) -> str:
        return "000"
