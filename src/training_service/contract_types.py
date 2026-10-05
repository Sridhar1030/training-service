"""External scalar type for the generator's unsupported URI composition."""

import re
from urllib.parse import urlsplit

from jsonschema import ValidationError
from pydantic import RootModel, StrictStr, field_validator

from training_service.contract import component_validator


class TrainingInputURI(RootModel[StrictStr]):
    """A scalar JSON string, not the object emitted by the stock generator."""

    @field_validator("root")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            if (
                not value.isascii()
                or re.search(r"%(?![0-9A-Fa-f]{2})", value)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Invalid training input URI")
            component_validator("TrainingInputURI").validate(value)
        except (ValidationError, ValueError) as error:
            raise ValueError("Invalid training input URI") from error
        return value

    def to_dict(self) -> str:
        return self.root
