from pydantic import BaseModel, Field

class GeocodeRequest(BaseModel):
    orgId: str = Field(..., min_length=3)
    venueId: str = Field(..., min_length=3)

class RecalcRequest(BaseModel):
    orgId: str = Field(..., min_length=3)
    date: str = Field(..., description="YYYY-MM-DD")

class DayLogisticsQuery(BaseModel):
    orgId: str
    date: str
