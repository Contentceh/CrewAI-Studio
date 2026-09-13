from typing import Optional, Dict, Any, Type
from crewai.tools import BaseTool
import requests
from pydantic import BaseModel, Field
from i18n import t


class CustomApiToolInputSchema(BaseModel):
    endpoint: str = Field(..., description=t('tool.endpoint'))
    method: str = Field(..., description=t('tool.http_method'))
    headers: Optional[Dict[str, str]] = Field(None, description=t('tool.http_headers'))
    query_params: Optional[Dict[str, Any]] = Field(None, description=t('tool.query_params'))
    body: Optional[Dict[str, Any]] = Field(None, description=t('tool.request_body'))

class CustomApiTool(BaseTool):
    name: str = t('tool.custom_api')
    description: str = t('tool.custom_api_desc')
    args_schema: Type[BaseModel] = CustomApiToolInputSchema
    base_url: Optional[str] = None
    default_headers: Optional[Dict[str, str]] = None
    default_query_params: Optional[Dict[str, Any]] = None

    def __init__(self, base_url: Optional[str] = None, headers: Optional[Dict[str, str]] = None, query_params: Optional[Dict[str, Any]] = None, **kwargs):
        super().__init__(**kwargs)
        self.base_url = base_url
        self.default_headers = headers or {}
        self.default_query_params = query_params or {}
        self._generate_description()
        

    def _run(self, endpoint: str, method: str, headers: Optional[Dict[str, str]] = None, query_params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint}".rstrip("/")
        headers = {**self.default_headers, **(headers or {})}
        query_params = {**self.default_query_params, **(query_params or {})}

        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                headers=headers,
                params=query_params,
                json=body,
                verify=True
            )
            return {
                "status_code": response.status_code,
                "response": response.json() if response.headers.get("Content-Type") == "application/json" else response.text
            }
        except Exception as e:
            return {
                "status_code": 500,
                "response": str(e)
            }

    def run(self, input_data: CustomApiToolInputSchema) -> Any:
        response_data = self._run(
            endpoint=input_data.endpoint,
            method=input_data.method,
            headers=input_data.headers,
            query_params=input_data.query_params,
            body=input_data.body
            
        )
        return response_data
