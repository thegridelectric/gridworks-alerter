from typing import Literal
from gwalerter.sema.base import SemaType
from gwalerter.sema.types.tank_temp_calibration import TankTempCalibration


class TankTempCalibrationMap(SemaType):
    """Sema: https://schemas.electricity.works/types/gw1.tank.temp.calibration.map/000"""

    buffer: TankTempCalibration
    tank: dict[str, TankTempCalibration]
    type_name: Literal["gw1.tank.temp.calibration.map"] = (
        "gw1.tank.temp.calibration.map"
    )
    version: Literal["000"] = "000"
