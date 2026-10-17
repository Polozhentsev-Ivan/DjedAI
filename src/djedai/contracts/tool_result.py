from pydantic import BaseModel, Field
from typing import Any, Optional, List, Dict

class ToolError(BaseModel):
    """
    Error class for tool results.
    """
    code: str = Field(description="Код ошибки (например, 'invalid_column')")
    message: str = Field(description="Текст ошибки для пользователя")
    suggestion: Optional[str] = Field(default=None, description="Подсказка, как исправить")

class ToolResult(BaseModel):
    """
    Universal contract for the response of any analytical tool.
    """
    
    result_id: str = Field(description="Уникальный ID результата, например 'r_17'")
    status: str = Field(description="Статус: 'ok', 'error' или 'empty'")
    
    # Данные и факты
    data: Optional[Dict[str, Any]] = Field(default=None, description="Данные (колонки и строки)")
    facts: List[Dict[str, Any]] = Field(default_factory=list, description="Факты для нейросети (текст + значения)")
    chart_hint: Optional[Dict[str, Any]] = Field(default=None, description="Подсказка для графика")
    
    # Ошибка
    error: Optional[ToolError] = Field(default=None, description="Объект ошибки, если status='error'")
    
    # Метаданные
    tool: Optional[str] = Field(default=None, description="Имя инструмента")
    params: Optional[Dict[str, Any]] = Field(default=None, description="Параметры, с которыми вызвали инструмент")
    sql: Optional[str] = Field(default=None, description="SQL-запрос, если он выполнялся")
    
    # Связь с предыдущими результатами (для задачи B2-3 и далее)
    source_result_id: Optional[str] = Field(default=None, description="ID результата, на основе которого сделан этот")