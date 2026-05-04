"""Pydantic models for Auth, Conversion & History"""
from pydantic import BaseModel, Field, model_validator, EmailStr
from typing import Optional, List, Dict, Any, Union
from datetime import datetime
from bson import ObjectId

# ============ AUTH MODELS ============
class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, pattern=r'^[a-zA-Z0-9_-]+$')
    email: EmailStr

class UserCreate(UserBase):
    """Request model for registration"""
    password: str = Field(..., min_length=8)

# Alias for API endpoint consistency
UserRegister = UserCreate

class UserInDB(UserBase):
    """Internal DB user model"""
    id: str = Field(alias="_id")
    hashed_password: str
    created_at: datetime
    is_active: bool = True
    scopes: List[str] = []
    provider: str = "password"
    google_id: Optional[str] = None
    avatar_url: Optional[str] = None
    updated_at: Optional[datetime] = None
    
    @model_validator(mode='before')
    @classmethod
    def parse_id(cls, data):
        if isinstance(data.get("_id"), ObjectId):
            data["_id"] = str(data["_id"])
        return data

class UserLogin(BaseModel):
    username: str
    password: str

class GoogleLoginRequest(BaseModel):
    credential: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    password: str = Field(..., min_length=8)

class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    scopes: List[str]
    created_at: datetime

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int

class TokenRefresh(BaseModel):
    refresh_token: str

class MessageResponse(BaseModel):
    message: str


# ============ CONVERSION HISTORY MODELS ============
class ConversionHistoryCreate(BaseModel):
    user_id: str
    curl_command: str
    python_code: Optional[str] = None
    parser_code: Optional[str] = None
    function_names: List[str] = []
    status: str = "success"
    error_message: Optional[str] = None
    request_type: str = "single"

class ConversionHistory(BaseModel):
    id: str = Field(alias="_id")
    user_id: str
    curl_command: str
    python_code: Optional[str] = None
    parser_code: Optional[str] = None
    function_names: List[str] = []
    status: str
    error_message: Optional[str] = None
    request_type: str
    created_at: datetime
    
    @model_validator(mode='before')
    @classmethod
    def parse_id(cls, data):
        if isinstance(data.get("_id"), ObjectId):
            data["_id"] = str(data["_id"])
        return data


# ============ API REQUEST/RESPONSE MODELS ============
class CurlRequest(BaseModel):
    curl: str = Field(..., description="The curl command to convert")
    function_name: Optional[str] = Field(None, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")

class ProxyConfig(BaseModel):
    enabled: bool = False
    http: str = ""
    https: str = ""

class ConvertRequest(BaseModel):
    collection_name: Optional[str] = None
    curl: Optional[Union[str, CurlRequest]] = None
    commands: Optional[List[Union[str, CurlRequest]]] = Field(None, min_length=1, max_length=50)
    function_name_prefix: Optional[str] = None
    proxy: Optional[ProxyConfig] = None
    
    @model_validator(mode='after')
    def validate_input(self):
        if self.curl is None and self.commands is None:
            raise ValueError("Either 'curl' or 'commands' must be provided")
        if self.curl is not None and self.commands is not None:
            raise ValueError("Provide either 'curl' OR 'commands', not both")
        return self
    
    def is_batch(self) -> bool:
        return self.commands is not None
    
    def get_commands(self) -> List[Dict[str, Any]]:
        if self.is_batch():
            return [self._normalize_cmd(cmd) for cmd in self.commands]
        return [self._normalize_cmd(self.curl)]
    
    def _normalize_cmd(self, cmd: Union[str, CurlRequest, Dict]) -> Dict[str, Any]:
        if isinstance(cmd, str): return {"curl": cmd}
        elif isinstance(cmd, CurlRequest): return cmd.model_dump(exclude_unset=True)
        elif isinstance(cmd, dict): return cmd
        return {"curl": str(getattr(cmd, 'curl', cmd))}

class ConversionResponse(BaseModel):
    success: bool
    python_code: Optional[str] = None
    parser_code: Optional[str] = None
    function_name: Optional[str] = None
    request_script: Optional[str] = None
    parser_script: Optional[str] = None
    function_names: List[str] = []
    error: Optional[str] = None
    error_type: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

class RunWorkspaceRequest(BaseModel):
    collection_name: Optional[str] = None
    workspace_name: str
    request_code: str
    parser_code: str
    proxy: Optional[ProxyConfig] = None

class RunWorkspaceResponse(BaseModel):
    success: bool
    workspace_name: str
    status: Optional[int] = None
    time_ms: int = 0
    size: str = "0 KB"
    content_type: str = ""
    extension: str = "json"
    response_file_name: Optional[str] = None
    file_name: Optional[str] = None
    response: Optional[Any] = None
    parsed: Optional[Any] = None
    logs: str = ""
    error: Optional[str] = None

class UserWorkspaceState(BaseModel):
    collections: Dict[str, Any] = {}
    activeCollectionId: Optional[str] = None
    theme: str = "dark"
    openResponseTabs: List[Dict[str, Any]] = []
    activeResponseTabId: Optional[str] = None
    updatedAt: Optional[datetime] = None

class HealthResponse(BaseModel):
    status: str
    version: str
    service: str = "curl-to-python-converter"
