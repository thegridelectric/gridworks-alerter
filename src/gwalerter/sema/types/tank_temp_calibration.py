from typing import Literal
from pydantic import StrictFloat
from gwalerter.sema.base import SemaType


class TankTempCalibration(SemaType):
    """Sema: https://schemas.electricity.works/types/gw1.tank.temp.calibration/000"""

    depth1_m: StrictFloat = 1.0
    depth1_b: StrictFloat = 0.0
    depth2_m: StrictFloat = 1.0
    depth2_b: StrictFloat = 0.0
    depth3_m: StrictFloat = 1.0
    depth3_b: StrictFloat = 0.0
    type_name: Literal["gw1.tank.temp.calibration"] = "gw1.tank.temp.calibration"
    version: Literal["000"] = "000"
