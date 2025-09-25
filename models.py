from __future__ import annotations
from pydantic import BaseModel, EmailStr, Field
from typing import Optional

class RegisterRow(BaseModel):
    passport: str = Field(..., description="Passport number")
    nationality: str = Field(..., description="Country of nationality as displayed on site (e.g., 'Bangladesh')")
    fullName: str
    gender: str = Field(..., description="M or F (or as per site)")
    dateOfBirth: str = Field(..., description="YYYY-MM-DD or accepted site format")
    passportExpiryDate: str = Field(..., description="YYYY-MM-DD or accepted site format")
    departureDate: str = Field(..., description="YYYY-MM-DD or accepted site format")
    arrivalDate: str = Field(..., description="YYYY-MM-DD or accepted site format")
    arrivalMode: str = "Air"
    flightNo: Optional[str] = None
    departureCountry: Optional[str] = None
    arrivalPoint: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    addressInMalaysia: Optional[str] = None
    purposeOfVisit: Optional[str] = "Tourism"

class PinRow(BaseModel):
    passport: str
    nationality: str
    pin: str