from typing import Literal
from gwalerter.sema.base import SemaType
from gwalerter.sema.enums import EmissionMethod
from gwalerter.sema.enums import Unit
from gwalerter.sema.property_format import LeftRightDot
from gwalerter.sema.property_format import SpaceheatName
from gwalerter.sema.property_format import UUID4Str
from gwalerter.sema.types.derived_channel_gt import DerivedChannelGt


class DerivedChannelGt000(SemaType):
    """Sema: https://schemas.electricity.works/types/derived.channel.gt/000"""

    id: UUID4Str
    name: SpaceheatName
    created_by_node_name: SpaceheatName
    strategy: SpaceheatName
    output_unit: Unit | None = None
    display_name: str
    terminal_asset_alias: LeftRightDot
    type_name: Literal["derived.channel.gt"] = "derived.channel.gt"
    version: Literal["000"] = "000"

    def upgrade(self) -> DerivedChannelGt:
        """
        - InputChannelNames[]: add
        - EmissionMethod: add
        - Parameters: add (Optional)
        """

        data = self.model_dump()
        data["input_channel_names"] = []
        data["emission_method"] = EmissionMethod.OnTrigger
        data["parameters"] = None
        data["version"] = "001"
        return DerivedChannelGt.model_validate(data)
