from enum import auto

from gwalerter.sema.enums.gw_str_enum import SemaEnum


class OpsgeniePriority(SemaEnum):
    """Sema: https://schemas.electricity.works/enums/gw.opsgenie.priority/000"""

    P1 = auto()
    P2 = auto()
    P3 = auto()
    P4 = auto()
    P5 = auto()

    @classmethod
    def default(cls) -> "OpsgeniePriority":
        return cls.P5

    @classmethod
    def values(cls) -> list[str]:
        return [elt.value for elt in cls]

    @classmethod
    def enum_name(cls) -> str:
        return "gw.opsgenie.priority"

    @classmethod
    def enum_version(cls) -> str:
        return "000"
