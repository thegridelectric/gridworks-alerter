from enum import auto

from gwalerter.sema.enums.gw_str_enum import SemaEnum


class OpsgenieAlertStatus(SemaEnum):
    """Sema: https://schemas.electricity.works/enums/gw.opsgenie.alert.status/000"""

    Open = auto()
    Closed = auto()

    @classmethod
    def default(cls) -> "OpsgenieAlertStatus":
        return cls.Open

    @classmethod
    def values(cls) -> list[str]:
        return [elt.value for elt in cls]

    @classmethod
    def enum_name(cls) -> str:
        return "gw.opsgenie.alert.status"

    @classmethod
    def enum_version(cls) -> str:
        return "000"
