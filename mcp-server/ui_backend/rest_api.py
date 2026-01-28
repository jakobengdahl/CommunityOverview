"""
UI Backend REST API - Endpoints for chat and document upload.

This module provides FastAPI endpoints for:
- /chat: Process chat messages with LLM and graph tools
- /upload: Upload and analyze documents

All graph mutations go through ChatService -> GraphService.
This module does NOT create graph objects directly.
"""

from typing import List, Dict, Any, Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import json

from .chat_service import ChatService
from .document_service import DocumentService


# ==================== Request/Response Models ====================

class ChatMessage(BaseModel):
    """A single chat message."""
    role: str = Field(..., description="Message role: 'user' or 'assistant'")
    content: Any = Field(..., description="Message content (string or content blocks)")


class ChatRequest(BaseModel):
    """Request body for /chat endpoint."""
    messages: List[ChatMessage] = Field(..., description="Conversation history")
    api_key: Optional[str] = Field(None, description="Optional API key override")
    provider: Optional[str] = Field(None, description="Optional provider: 'claude' or 'openai'")


class SimpleChatRequest(BaseModel):
    """Simplified chat request with just a message."""
    message: str = Field(..., description="User's message")
    conversation_id: Optional[str] = Field(None, description="Optional conversation ID for context")
    api_key: Optional[str] = Field(None, description="Optional API key override")
    provider: Optional[str] = Field(None, description="Optional provider: 'claude' or 'openai'")


class ChatResponse(BaseModel):
    """Response from /chat endpoint."""
    content: str = Field(..., description="LLM response text")
    toolUsed: Optional[str] = Field(None, description="Name of tool used (if any)")
    toolResult: Optional[Any] = Field(None, description="Result from tool (if any)")


class UploadResponse(BaseModel):
    """Response from /upload endpoint."""
    success: bool
    filename: Optional[str] = None
    text: Optional[str] = None
    char_count: Optional[int] = None
    word_count: Optional[int] = None
    error: Optional[str] = None
    chat_response: Optional[ChatResponse] = None


# ==================== Router Factory ====================

def create_ui_router(
    chat_service: ChatService,
    document_service: Optional[DocumentService] = None
) -> APIRouter:
    """
    Create the UI backend router with chat and upload endpoints.

    Args:
        chat_service: ChatService instance for handling chat
        document_service: Optional DocumentService for file uploads

    Returns:
        Configured APIRouter
    """
    router = APIRouter(tags=["UI Backend"])

    # Create document service if not provided
    if document_service is None:
        document_service = DocumentService()

    # ==================== Chat Endpoints ====================

    @router.post("/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest) -> ChatResponse:
        """
        Process a chat message with the LLM.

        The LLM may use graph tools to answer queries or modify the graph.
        All graph operations go through GraphService.

        Args:
            request: Chat request with messages and optional config

        Returns:
            LLM response with any tool results
        """
        try:
            # Convert messages to dict format
            messages = [msg.model_dump() for msg in request.messages]

            result = chat_service.process_message(
                messages=messages,
                api_key=request.api_key,
                provider=request.provider
            )

            return ChatResponse(
                content=result.get("content", ""),
                toolUsed=result.get("toolUsed"),
                toolResult=result.get("toolResult")
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/chat/simple", response_model=ChatResponse)
    async def chat_simple(request: SimpleChatRequest) -> ChatResponse:
        """
        Simplified chat endpoint for single messages.

        This endpoint is easier to use for simple queries.
        For conversation context, use the full /chat endpoint.

        Args:
            request: Simple chat request with message

        Returns:
            LLM response with any tool results
        """
        try:
            result = chat_service.process_chat_request(
                user_message=request.message,
                api_key=request.api_key,
                provider=request.provider
            )

            return ChatResponse(
                content=result.get("content", ""),
                toolUsed=result.get("toolUsed"),
                toolResult=result.get("toolResult")
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ==================== Upload Endpoints ====================

    @router.post("/upload", response_model=UploadResponse)
    async def upload_document(
        file: UploadFile = File(...),
        message: Optional[str] = Form(None),
        analyze: bool = Form(True),
        api_key: Optional[str] = Form(None),
        provider: Optional[str] = Form(None)
    ) -> UploadResponse:
        """
        Upload and optionally analyze a document.

        Supported formats: PDF, Word (.docx, .doc), Text (.txt)

        The document text is extracted and can optionally be sent
        to the LLM for analysis. Any graph modifications requested
        by the LLM go through GraphService.

        Args:
            file: The uploaded file
            message: Optional message/question about the document
            analyze: Whether to analyze with LLM (default True)
            api_key: Optional API key override
            provider: Optional LLM provider

        Returns:
            Extracted text and optional LLM analysis
        """
        try:
            # Read file content
            content = await file.read()

            # Process the upload and extract text
            result = await document_service.process_upload(
                file_content=content,
                filename=file.filename or "unknown"
            )

            if not result["success"]:
                return UploadResponse(
                    success=False,
                    error=result.get("error"),
                    filename=result.get("filename")
                )

            response = UploadResponse(
                success=True,
                filename=result.get("filename"),
                text=result.get("text"),
                char_count=result.get("char_count"),
                word_count=result.get("word_count")
            )

            # Optionally analyze with LLM
            if analyze and result.get("text"):
                user_message = message or "Please analyze this document and summarize its main points."

                chat_result = chat_service.process_chat_request(
                    user_message=user_message,
                    document_context=result["text"],
                    api_key=api_key,
                    provider=provider
                )

                response.chat_response = ChatResponse(
                    content=chat_result.get("content", ""),
                    toolUsed=chat_result.get("toolUsed"),
                    toolResult=chat_result.get("toolResult")
                )

            return response

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/upload/extract")
    async def extract_only(
        file: UploadFile = File(...)
    ) -> Dict[str, Any]:
        """
        Extract text from a document without LLM analysis.

        Use this endpoint when you only need the extracted text
        and want to handle analysis separately.

        Args:
            file: The uploaded file

        Returns:
            Extracted text and metadata
        """
        try:
            content = await file.read()

            result = await document_service.process_upload(
                file_content=content,
                filename=file.filename or "unknown"
            )

            return result

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ==================== Info Endpoints ====================

    @router.get("/info")
    async def get_info() -> Dict[str, Any]:
        """
        Get information about the UI backend configuration.

        Returns:
            Provider info, available tools, and graph stats
        """
        return chat_service.get_system_info()

    @router.get("/supported-formats")
    async def get_supported_formats() -> Dict[str, Any]:
        """
        Get supported document formats for upload.

        Returns:
            List of supported file extensions
        """
        return {
            "formats": list(document_service.SUPPORTED_EXTENSIONS),
            "max_size_mb": document_service.MAX_FILE_SIZE / 1024 / 1024
        }

    return router
