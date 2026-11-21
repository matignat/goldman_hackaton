"""Validated personal details for staff accounts."""

from pydantic import BaseModel, ConfigDict, Field


class ProfileData(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+$")
    phone: str = Field(default="", max_length=50)
