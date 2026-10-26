"""Archives original permit report PDFs in Azure Blob Storage."""

from __future__ import annotations

from collections.abc import Mapping

from .config import load_settings


DEFAULT_CONTAINER = "permit-reports"


class BlobPdfArchive:
    def __init__(self, container_client) -> None:
        self.container_client = container_client

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> BlobPdfArchive:
        env = load_settings(env)
        account_url = env.get("AZURE_STORAGE_ACCOUNT_URL")
        if not account_url:
            raise ValueError("Set AZURE_STORAGE_ACCOUNT_URL to enable PDF archiving")

        try:
            from azure.core.exceptions import ResourceExistsError
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
        except ImportError as error:
            raise RuntimeError("Install azure-identity and azure-storage-blob to archive PDFs") from error

        service = BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())
        container = service.get_container_client(env.get("AZURE_STORAGE_CONTAINER", DEFAULT_CONTAINER))
        try:
            container.create_container()
        except ResourceExistsError:
            pass
        return cls(container)

    def archive_pdf(self, content: bytes, category: str, content_sha256: str) -> str:
        from azure.storage.blob import ContentSettings

        blob_name = f"pdfs/{category}/{content_sha256[:2]}/{content_sha256}.pdf"
        blob = self.container_client.get_blob_client(blob_name)
        blob.upload_blob(
            content,
            overwrite=True,
            content_settings=ContentSettings(content_type="application/pdf"),
        )
        return blob.url