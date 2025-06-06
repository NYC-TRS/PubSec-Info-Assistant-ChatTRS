# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
from io import StringIO
from typing import Optional
import asyncio
# from sse_starlette.sse import EventSourceResponse
# from starlette.responses import StreamingResponse
from starlette.responses import Response
import logging  # Added
from datetime import datetime, time, timedelta  # Added datetime
import os
import json
import urllib.parse
import pandas as pd
from fastapi.staticfiles import StaticFiles
# Added Request, Form
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, Form
from fastapi.responses import RedirectResponse, StreamingResponse
import openai
from approaches.comparewebwithwork import CompareWebWithWork
from approaches.compareworkwithweb import CompareWorkWithWeb
from approaches.chatreadretrieveread import ChatReadRetrieveReadApproach
from approaches.chatwebretrieveread import ChatWebRetrieveRead
from approaches.gpt_direct_approach import GPTDirectApproach
from approaches.approach import Approaches
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, AzureAuthorityHosts
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from azure.search.documents import SearchClient
from azure.storage.blob import (
    AccountSasPermissions,
    BlobServiceClient,
    ResourceTypes,
    generate_account_sas,
    ContentSettings  # Added for file upload
)

from approaches.tabulardataassistant import (
    refreshagent,
    save_df,
    process_agent_response as td_agent_response,
    process_agent_scratch_pad as td_agent_scratch_pad,
    get_images_in_temp

)
from shared_code.status_log import State, StatusClassification, StatusLog, StatusQueryLevel
from azure.cosmos import CosmosClient


# === ENV Setup ===

ENV = {
    "AZURE_BLOB_STORAGE_ACCOUNT": None,
    "AZURE_BLOB_STORAGE_ENDPOINT": None,
    "AZURE_BLOB_STORAGE_KEY": None,
    "AZURE_BLOB_STORAGE_CONTAINER": "content",
    "AZURE_BLOB_STORAGE_UPLOAD_CONTAINER": "upload",
    "AZURE_SEARCH_SERVICE": "gptkb",
    "AZURE_SEARCH_SERVICE_ENDPOINT": None,
    "AZURE_SEARCH_SERVICE_KEY": None,
    "AZURE_SEARCH_INDEX": "gptkbindex",
    "USE_SEMANTIC_RERANKER": "true",
    "AZURE_OPENAI_SERVICE": "myopenai",
    "AZURE_OPENAI_RESOURCE_GROUP": "",
    "AZURE_OPENAI_ENDPOINT": "",
    "AZURE_OPENAI_AUTHORITY_HOST": "AzureCloud",
    "AZURE_OPENAI_CHATGPT_DEPLOYMENT": "gpt-35-turbo-16k",
    "AZURE_OPENAI_CHATGPT_MODEL_NAME": "",
    "AZURE_OPENAI_CHATGPT_MODEL_VERSION": "",
    "USE_AZURE_OPENAI_EMBEDDINGS": "false",
    "EMBEDDING_DEPLOYMENT_NAME": "",
    "AZURE_OPENAI_EMBEDDINGS_MODEL_NAME": "",
    "AZURE_OPENAI_EMBEDDINGS_VERSION": "",
    "AZURE_OPENAI_SERVICE_KEY": None,
    "AZURE_SUBSCRIPTION_ID": None,
    "AZURE_ARM_MANAGEMENT_API": "https://management.azure.com",
    "CHAT_WARNING_BANNER_TEXT": "",
    "APPLICATION_TITLE": "Information Assistant, built with Azure OpenAI",
    "KB_FIELDS_CONTENT": "content",
    "KB_FIELDS_PAGENUMBER": "pages",
    "KB_FIELDS_SOURCEFILE": "file_uri",
    "KB_FIELDS_CHUNKFILE": "chunk_file",
    "COSMOSDB_URL": None,
    "COSMOSDB_KEY": None,
    "COSMOSDB_LOG_DATABASE_NAME": "statusdb",
    "COSMOSDB_LOG_CONTAINER_NAME": "statuscontainer",
    "QUERY_TERM_LANGUAGE": "English",
    "TARGET_EMBEDDINGS_MODEL": "BAAI/bge-small-en-v1.5",
    "ENRICHMENT_APPSERVICE_URL": "enrichment",
    "TARGET_TRANSLATION_LANGUAGE": "en",
    "ENRICHMENT_ENDPOINT": None,
    "ENRICHMENT_KEY": None,
    "AZURE_AI_TRANSLATION_DOMAIN": "api.cognitive.microsofttranslator.com",
    "BING_SEARCH_ENDPOINT": "https://api.bing.microsoft.com/",
    "BING_SEARCH_KEY": "",
    "ENABLE_BING_SAFE_SEARCH": "true",
    "ENABLE_WEB_CHAT": "false",
    "ENABLE_UNGROUNDED_CHAT": "false",
    "ENABLE_MATH_ASSISTANT": "false",
    "ENABLE_TABULAR_DATA_ASSISTANT": "false",
    "ENABLE_MULTIMEDIA": "false",
    "MAX_CSV_FILE_SIZE": "7"
}

for key, value in ENV.items():
    new_value = os.getenv(key)
    if new_value is not None:
        ENV[key] = new_value
    elif value is None:
        raise ValueError(f"Environment variable {key} not set")

str_to_bool = {'true': True, 'false': False}

# --- Logging Setup ---
# Basic configuration, Azure App Service might provide more advanced integration via APPINSIGHTS_INSTRUMENTATIONKEY
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)  # Use __name__ for the logger
# Set level from env var or default to INFO
log.setLevel(ENV.get("LOG_LEVEL", "INFO").upper())
log.propagate = True

# Simple in-memory set to track users seen in this instance/session to avoid re-logging start
# NOTE: This won't persist across restarts or multiple server instances.
users_seen_this_session = set()
# --- End Logging Setup ---


dffinal = None
# Used by the OpenAI SDK
openai.api_type = "azure"
openai.api_base = ENV["AZURE_OPENAI_ENDPOINT"]
if ENV["AZURE_OPENAI_AUTHORITY_HOST"] == "AzureUSGovernment":
    AUTHORITY = AzureAuthorityHosts.AZURE_GOVERNMENT
else:
    AUTHORITY = AzureAuthorityHosts.AZURE_PUBLIC_CLOUD
openai.api_version = "2024-02-01"
# Use the current user identity to authenticate with Azure OpenAI, Cognitive Search and Blob Storage (no secrets needed,
# just use 'az login' locally, and managed identity when deployed on Azure). If you need to use keys, use separate AzureKeyCredential instances with the
# keys for each service
# If you encounter a blocking error during a DefaultAzureCredntial resolution, you can exclude the problematic credential by using a parameter (ex. exclude_shared_token_cache_credential=True)
azure_credential = DefaultAzureCredential(authority=AUTHORITY)
# Comment these two lines out if using keys, set your API key in the OPENAI_API_KEY environment variable instead
# openai.api_type = "azure_ad"
# openai_token = azure_credential.get_token("https://cognitiveservices.azure.com/.default")
openai.api_key = ENV["AZURE_OPENAI_SERVICE_KEY"]

# Setup StatusLog to allow access to CosmosDB for logging
statusLog = StatusLog(
    ENV["COSMOSDB_URL"],
    ENV["COSMOSDB_KEY"],
    ENV["COSMOSDB_LOG_DATABASE_NAME"],
    ENV["COSMOSDB_LOG_CONTAINER_NAME"]
)

azure_search_key_credential = AzureKeyCredential(
    ENV["AZURE_SEARCH_SERVICE_KEY"])
# Set up clients for Cognitive Search and Storage
search_client = SearchClient(
    endpoint=ENV["AZURE_SEARCH_SERVICE_ENDPOINT"],
    index_name=ENV["AZURE_SEARCH_INDEX"],
    credential=azure_search_key_credential,
)
blob_client = BlobServiceClient(
    account_url=ENV["AZURE_BLOB_STORAGE_ENDPOINT"],
    credential=ENV["AZURE_BLOB_STORAGE_KEY"],
)
blob_container = blob_client.get_container_client(
    ENV["AZURE_BLOB_STORAGE_CONTAINER"])
# Get client for upload container (needed for file upload endpoint)
blob_upload_container_client = blob_client.get_container_client(
    ENV["AZURE_BLOB_STORAGE_UPLOAD_CONTAINER"])


model_name = ''
model_version = ''

# Set up OpenAI management client
openai_mgmt_client = CognitiveServicesManagementClient(
    credential=azure_credential,
    subscription_id=ENV["AZURE_SUBSCRIPTION_ID"],
    base_url=ENV["AZURE_ARM_MANAGEMENT_API"],
    credential_scopes=[ENV["AZURE_ARM_MANAGEMENT_API"] + "/.default"])

deployment = openai_mgmt_client.deployments.get(
    resource_group_name=ENV["AZURE_OPENAI_RESOURCE_GROUP"],
    account_name=ENV["AZURE_OPENAI_SERVICE"],
    deployment_name=ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"])

model_name = deployment.properties.model.name
model_version = deployment.properties.model.version

if (str_to_bool.get(ENV["USE_AZURE_OPENAI_EMBEDDINGS"])):
    embedding_deployment = openai_mgmt_client.deployments.get(
        resource_group_name=ENV["AZURE_OPENAI_RESOURCE_GROUP"],
        account_name=ENV["AZURE_OPENAI_SERVICE"],
        deployment_name=ENV["EMBEDDING_DEPLOYMENT_NAME"])

    embedding_model_name = embedding_deployment.properties.model.name
    embedding_model_version = embedding_deployment.properties.model.version
else:
    embedding_model_name = ""
    embedding_model_version = ""

chat_approaches = {
    Approaches.ReadRetrieveRead: ChatReadRetrieveReadApproach(
        search_client,
        ENV["AZURE_OPENAI_ENDPOINT"],
        ENV["AZURE_OPENAI_SERVICE_KEY"],
        ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        ENV["KB_FIELDS_SOURCEFILE"],
        ENV["KB_FIELDS_CONTENT"],
        ENV["KB_FIELDS_PAGENUMBER"],
        ENV["KB_FIELDS_CHUNKFILE"],
        ENV["AZURE_BLOB_STORAGE_CONTAINER"],
        blob_client,
        ENV["QUERY_TERM_LANGUAGE"],
        model_name,
        model_version,
        ENV["TARGET_EMBEDDINGS_MODEL"],
        ENV["ENRICHMENT_APPSERVICE_URL"],
        ENV["TARGET_TRANSLATION_LANGUAGE"],
        ENV["ENRICHMENT_ENDPOINT"],
        ENV["ENRICHMENT_KEY"],
        ENV["AZURE_AI_TRANSLATION_DOMAIN"],
        str_to_bool.get(ENV["USE_SEMANTIC_RERANKER"])
    ),
    Approaches.ChatWebRetrieveRead: ChatWebRetrieveRead(
        model_name,
        ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        ENV["TARGET_TRANSLATION_LANGUAGE"],
        ENV["BING_SEARCH_ENDPOINT"],
        ENV["BING_SEARCH_KEY"],
        str_to_bool.get(ENV["ENABLE_BING_SAFE_SEARCH"])
    ),
    Approaches.CompareWorkWithWeb: CompareWorkWithWeb(
        model_name,
        ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        ENV["TARGET_TRANSLATION_LANGUAGE"],
        ENV["BING_SEARCH_ENDPOINT"],
        ENV["BING_SEARCH_KEY"],
        str_to_bool.get(ENV["ENABLE_BING_SAFE_SEARCH"])
    ),
    Approaches.CompareWebWithWork: CompareWebWithWork(
        search_client,
        ENV["AZURE_OPENAI_ENDPOINT"],
        ENV["AZURE_OPENAI_SERVICE_KEY"],
        ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        ENV["KB_FIELDS_SOURCEFILE"],
        ENV["KB_FIELDS_CONTENT"],
        ENV["KB_FIELDS_PAGENUMBER"],
        ENV["KB_FIELDS_CHUNKFILE"],
        ENV["AZURE_BLOB_STORAGE_CONTAINER"],
        blob_client,
        ENV["QUERY_TERM_LANGUAGE"],
        model_name,
        model_version,
        ENV["TARGET_EMBEDDINGS_MODEL"],
        ENV["ENRICHMENT_APPSERVICE_URL"],
        ENV["TARGET_TRANSLATION_LANGUAGE"],
        ENV["ENRICHMENT_ENDPOINT"],
        ENV["ENRICHMENT_KEY"],
        ENV["AZURE_AI_TRANSLATION_DOMAIN"],
        str_to_bool.get(ENV["USE_SEMANTIC_RERANKER"])
    ),
    Approaches.GPTDirect: GPTDirectApproach(
        ENV["AZURE_OPENAI_SERVICE"],
        ENV["AZURE_OPENAI_SERVICE_KEY"],
        ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        ENV["QUERY_TERM_LANGUAGE"],
        model_name,
        model_version,
        ENV["AZURE_OPENAI_ENDPOINT"]
    )
}

# --- Middleware Definition ---


async def log_session_start_middleware(request: Request, call_next):
    global users_seen_this_session
    username = None
    # Azure App Service Authentication typically injects user info in headers.
    # Common headers are X-MS-CLIENT-PRINCIPAL-NAME (often email/UPN)
    # or X-MS-CLIENT-PRINCIPAL-ID (object ID). Adjust if your setup uses different ones.
    try:
        principal_name_header = "x-ms-client-principal-name"  # Case-insensitive check
        if principal_name_header in request.headers:
            username = request.headers[principal_name_header]

        # Simple check to log only once per user per instance lifetime
        if username and username not in users_seen_this_session:
            timestamp = datetime.now().isoformat()
            log.info(
                f"User session started: User={username}, Timestamp={timestamp}")
            # Add user to prevent re-logging for this instance
            users_seen_this_session.add(username)

    except Exception as e:
        # Log errors but don't block requests
        log.error(f"Error in session logging middleware: {e}")

    response = await call_next(request)
    return response
# --- End Middleware Definition ---


# Create API
app = FastAPI(
    title="IA Web API",
    description="A Python API to serve as Backend For the Information Assistant Web App",
    version="0.1.0",
    docs_url="/docs",
)

# Add the session logging middleware
app.middleware("http")(log_session_start_middleware)


@app.get("/", include_in_schema=False, response_class=RedirectResponse)
async def root():
    """Redirect to the index.html page"""
    return RedirectResponse(url="/index.html")

# --- Function to wrap the stream, log the full response, and yield chunks ---
async def log_streaming_response(stream_generator, username, timestamp, approach):
    response_chunks = []
    full_response_content = ""
    try:
        async for chunk_str in stream_generator:
            response_chunks.append(chunk_str) # Store the raw chunk string
            try:
                # Attempt to parse and extract content for logging
                chunk_json = json.loads(chunk_str)
                if "content" in chunk_json and chunk_json["content"]:
                    full_response_content += chunk_json["content"]
            except json.JSONDecodeError:
                # Handle cases where a chunk might not be valid JSON or doesn't contain 'content'
                log.debug(f"Could not parse chunk for logging: {chunk_str}")
            except Exception as e:
                 log.error(f"Error processing chunk for logging: {e}")
            yield chunk_str # Yield the original chunk to the client
    finally:
        # This block executes after the stream generator is exhausted or if an error occurs during streaming
        # Log the complete accumulated response
        # Replace newline characters for better readability in logs if desired
        log_content = full_response_content.replace("\n", "\\n")
        log.info(f"Bot response complete: User={username}, Timestamp={timestamp}, Approach={approach}, Response='{log_content}'")
# --- End Stream Wrapping Function ---


@app.post("/chat")
async def chat(request: Request):
    """Chat with the bot using a given approach"""
    json_body = await request.json()
    approach = json_body.get("approach")

    # <<< --- ADD USER QUESTION LOGGING HERE --- >>>
    username = "unknown_user"  # Default username
    timestamp = datetime.now().isoformat()  # Timestamp for logging
    try:
        # Attempt to get username from header
        username = request.headers.get(
            "x-ms-client-principal-name", "unknown_user")
        # Get last user message from history
        user_message = json_body.get(
            "history", [{}])[-1].get("user", "no_message")
        # Log the user question
        log.info(
            f"User question: User={username}, Timestamp={timestamp}, Message='{user_message}'")
    except Exception as e:
        log.error(f"Error logging user question: {e}")
    # <<< --- END USER QUESTION LOGGING --- >>>

    try:
        impl = chat_approaches.get(Approaches(int(approach)))
        if not impl:
            log.error(
                f"Unknown approach selected: User={username}, Timestamp={timestamp}, Approach={approach}")
            return {"error": "unknown approach"}, 400

        # <<< --- ADD BOT RESPONSE START LOGGING HERE --- >>>
        
        # <<< --- END BOT RESPONSE START LOGGING --- >>>

        # Existing logic to run the approach and get the response stream 'r'
        if (Approaches(int(approach)) == Approaches.CompareWorkWithWeb or Approaches(int(approach)) == Approaches.CompareWebWithWork):
            r = impl.run(json_body.get("history", []), json_body.get("overrides", {
            }), json_body.get("citation_lookup", {}), json_body.get("thought_chain", {}))
        else:
            r = impl.run(json_body.get("history", []), json_body.get(
                "overrides", {}), {}, json_body.get("thought_chain", {}))

        # Wrap the stream generator 'r' with the logging function
        logged_stream = log_streaming_response(r, username, timestamp, approach)
        return StreamingResponse(logged_stream, media_type="application/x-ndjson")

    except Exception as ex:
        # Log the error during chat processing
        log.error(
            f"Chat processing error: User={username}, Timestamp={timestamp}, Approach={approach}, Error={str(ex)}")
        # Raise HTTPException for FastAPI to handle
        raise HTTPException(status_code=500, detail=str(ex)) from ex

@app.get("/getblobclienturl")
async def get_blob_client_url():
    """Get a URL for a file in Blob Storage with SAS token."""
    sas_token = generate_account_sas(
        ENV["AZURE_BLOB_STORAGE_ACCOUNT"],
        ENV["AZURE_BLOB_STORAGE_KEY"],
        resource_types=ResourceTypes(
            object=True, service=True, container=True),
        permission=AccountSasPermissions(
            read=True,
            write=True,
            list=True,
            delete=False,
            add=True,
            create=True,
            update=True,
            process=False,
        ),
        expiry=datetime.utcnow() + timedelta(hours=1),
    )
    return {"url": f"{blob_client.url}?{sas_token}"}


@app.post("/getalluploadstatus")
async def get_all_upload_status(request: Request):
    """Get the status and tags of all file uploads."""
    json_body = await request.json()
    timeframe = json_body.get("timeframe")
    state = json_body.get("state")
    folder = json_body.get("folder")
    tag = json_body.get("tag")
    try:
        results = statusLog.read_files_status_by_timeframe(timeframe,
                                                           State[state],
                                                           folder,
                                                           tag,
                                                           os.environ["AZURE_BLOB_STORAGE_UPLOAD_CONTAINER"])
        # The rest of the tag retrieval seems redundant if read_files_status_by_timeframe already returns tags.
        # Consider simplifying if StatusLog class handles this efficiently.
        # Keeping original logic for now:
        items = []
        cosmos_client = CosmosClient(
            url=statusLog._url, credential=statusLog._key)
        database = cosmos_client.get_database_client(statusLog._database_name)
        container = database.get_container_client(statusLog._container_name)
        query_string = "SELECT DISTINCT VALUE t FROM c JOIN t IN c.tags"
        items = list(container.query_items(
            query=query_string,
            enable_cross_partition_query=True
        ))
        unique_tags = set()
        for item in items:
            tags = item.split(',')
            unique_tags.update(tags)

    except Exception as ex:
        log.exception("Exception in /getalluploadstatus")
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    return results


@app.post("/getfolders")
async def get_folders(request: Request):
    """Get all folders."""
    try:
        blob_container_client = blob_client.get_container_client(
            os.environ["AZURE_BLOB_STORAGE_UPLOAD_CONTAINER"])
        folders = set()  # Use a set for automatic uniqueness
        blob_list = blob_container_client.list_blobs()
        for blob in blob_list:
            folder_path = os.path.dirname(blob.name)
            if folder_path:  # Add only if there's a folder path
                folders.add(folder_path)
    except Exception as ex:
        log.exception("Exception in /getfolders")
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    return list(folders)  # Convert set back to list


@app.post("/deleteItems")
async def delete_Items(request: Request):
    """Delete a blob."""
    json_body = await request.json()
    full_path = json_body.get("path")
    path = full_path.split("/", 1)[1]  # remove the container prefix
    try:
        blob_container_client = blob_client.get_container_client(
            os.environ["AZURE_BLOB_STORAGE_UPLOAD_CONTAINER"])
        blob_container_client.delete_blob(path)
        statusLog.upsert_document(document_path=full_path,
                                  status='Delete initiated',
                                  status_classification=StatusClassification.INFO,
                                  state=State.DELETING,
                                  fresh_start=False)
        statusLog.save_document(document_path=full_path)

    except Exception as ex:
        log.exception("Exception in /delete_Items")
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    return True


@app.post("/resubmitItems")
async def resubmit_Items(request: Request):
    """Resubmit a blob."""
    json_body = await request.json()
    path = json_body.get("path")
    path = path.split("/", 1)[1]  # remove the container prefix
    try:
        blob_container_client = blob_client.get_container_client(
            os.environ["AZURE_BLOB_STORAGE_UPLOAD_CONTAINER"])
        blob_data = blob_container_client.download_blob(path).readall()
        # Re-upload to potentially trigger processing again (assuming blob trigger)
        blob_container_client.upload_blob(
            name=path, data=blob_data, overwrite=True)
        full_path = os.environ["AZURE_BLOB_STORAGE_UPLOAD_CONTAINER"] + '/' + path
        statusLog.upsert_document(document_path=full_path,
                                  status='Resubmitted to the processing pipeline',
                                  status_classification=StatusClassification.INFO,
                                  state=State.QUEUED,
                                  fresh_start=False)  # Use False to update existing log
        statusLog.save_document(document_path=full_path)

    except Exception as ex:
        log.exception("Exception in /resubmitItems")
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    return True


@app.post("/gettags")
async def get_tags(request: Request):
    """Get all unique tags."""
    try:
        cosmos_client = CosmosClient(
            url=statusLog._url, credential=statusLog._key)
        database = cosmos_client.get_database_client(statusLog._database_name)
        container = database.get_container_client(statusLog._container_name)
        query_string = "SELECT DISTINCT VALUE t FROM c JOIN t IN c.tags"
        items = list(container.query_items(
            query=query_string,
            enable_cross_partition_query=True
        ))
        unique_tags = set()
        for item in items:
            tags = item.split(',')
            unique_tags.update(tags)

    except Exception as ex:
        log.exception("Exception in /gettags")
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    return list(unique_tags)  # Convert set back to list


@app.post("/logstatus")
async def logstatus(request: Request):
    """Log the status of a file upload."""
    try:
        json_body = await request.json()
        path = json_body.get("path")
        status = json_body.get("status")
        status_classification_str = json_body.get(
            "status_classification", "INFO").upper()
        state_str = json_body.get("state", "PROCESSING").upper()

        # Safely get enum members, defaulting to INFO/PROCESSING if invalid
        status_classification = StatusClassification[
            status_classification_str] if status_classification_str in StatusClassification.__members__ else StatusClassification.INFO
        state = State[state_str] if state_str in State.__members__ else State.PROCESSING

        statusLog.upsert_document(document_path=path,
                                  status=status,
                                  status_classification=status_classification,
                                  state=state,
                                  fresh_start=True)  # Assuming this endpoint always starts a log
        statusLog.save_document(document_path=path)

    except Exception as ex:
        log.exception("Exception in /logstatus")
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    # Return success explicitly
    return {"status": "success"}


@app.get("/getInfoData")
async def get_info_data():
    """Get application info data."""
    response = {
        "AZURE_OPENAI_CHATGPT_DEPLOYMENT": ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        "AZURE_OPENAI_MODEL_NAME": f"{model_name}",
        "AZURE_OPENAI_MODEL_VERSION": f"{model_version}",
        "AZURE_OPENAI_SERVICE": ENV["AZURE_OPENAI_SERVICE"],
        "AZURE_SEARCH_SERVICE": ENV["AZURE_SEARCH_SERVICE"],
        "AZURE_SEARCH_INDEX": ENV["AZURE_SEARCH_INDEX"],
        "TARGET_LANGUAGE": ENV["QUERY_TERM_LANGUAGE"],
        "USE_AZURE_OPENAI_EMBEDDINGS": ENV["USE_AZURE_OPENAI_EMBEDDINGS"],
        "EMBEDDINGS_DEPLOYMENT": ENV["EMBEDDING_DEPLOYMENT_NAME"],
        "EMBEDDINGS_MODEL_NAME": f"{embedding_model_name}",
        "EMBEDDINGS_MODEL_VERSION": f"{embedding_model_version}",
    }
    return response


@app.get("/getWarningBanner")
async def get_warning_banner():
    """Get the warning banner text."""
    return {"WARNING_BANNER_TEXT": ENV["CHAT_WARNING_BANNER_TEXT"]}


@app.get("/getMaxCSVFileSize")
async def get_max_csv_file_size():
    """Get the max csv size."""
    return {"MAX_CSV_FILE_SIZE": ENV["MAX_CSV_FILE_SIZE"]}


@app.post("/getcitation")
async def get_citation(request: Request):
    """Get the citation content for a given chunk file path."""
    try:
        json_body = await request.json()
        # Citation path usually comes URL encoded from the frontend
        citation_path_encoded = json_body.get("citation")
        if not citation_path_encoded:
            raise HTTPException(
                status_code=400, detail="Citation path is required")

        # Decode the URL-encoded path
        citation_path_decoded = urllib.parse.unquote(citation_path_encoded)

        # Assuming citation_path_decoded is the full path like 'container/folder/chunk.json'
        blob = blob_container.get_blob_client(
            citation_path_decoded).download_blob()
        decoded_text = blob.readall().decode('utf-8')  # Specify encoding
        results = json.loads(decoded_text)
    except Exception as ex:
        log.exception(
            f"Exception in /getcitation for path: {citation_path_encoded}")
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    return results

# Return APPLICATION_TITLE


@app.get("/getApplicationTitle")
async def get_application_title():
    """Get the application title text."""
    return {"APPLICATION_TITLE": ENV["APPLICATION_TITLE"]}


@app.get("/getalltags")
async def get_all_tags():
    """Get all unique tags from status logs."""
    try:
        results = statusLog.get_all_tags()
        # get_all_tags currently returns a comma-separated string, convert to list
        tag_list = list(set(results.split(','))) if results else []
    except Exception as ex:
        log.exception("Exception in /getalltags")
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    return tag_list


@app.get("/getTempImages")
async def get_temp_images():
    """Get base64 encoded images from the temp directory."""
    images = get_images_in_temp()
    return {"images": images}


# --- Tabular Data Assistant Endpoints ---
@app.post("/posttd")
async def posttd(csv: UploadFile = File(...)):
    global dffinal
    try:
        content = await csv.read()
        # Use latin-1 encoding for broader compatibility, consider utf-8-sig if BOM is present
        df = pd.read_csv(StringIO(content.decode('latin-1')))
        dffinal = df
        save_df(df)  # Assuming save_df stores it appropriately for the agent
    except Exception as ex:
        log.exception(f"Error processing uploaded CSV: {csv.filename}")
        raise HTTPException(
            status_code=500, detail=f"Error processing CSV: {str(ex)}") from ex
    return {"filename": csv.filename, "message": "CSV processed successfully"}


@app.get("/process_td_agent_response")
async def process_td_agent_response(retries=3, delay=1, question: Optional[str] = None):
    if question is None:
        raise HTTPException(status_code=400, detail="Question is required")
    if dffinal is None:
        return ["error: CSV has not been loaded or processed yet."]

    for i in range(retries):
        try:
            results = td_agent_response(question, dffinal)  # Pass dffinal
            return results
        except AttributeError as ex:
            log.exception(
                f"Exception in /process_td_agent_response: {str(ex)}")
            if i < retries - 1:
                await asyncio.sleep(delay)
            # More specific check
            elif "'NoneType' object has no attribute 'stream'" in str(ex):
                return ["error: CSV related error, potentially not loaded correctly."]
            else:
                raise HTTPException(status_code=500, detail=str(ex)) from ex
        except Exception as ex:
            log.exception(
                f"Exception in /process_td_agent_response: {str(ex)}")
            if i < retries - 1:
                await asyncio.sleep(delay)
            else:
                raise HTTPException(status_code=500, detail=str(ex)) from ex


@app.get("/getTdAnalysis")
async def getTdAnalysis(retries=3, delay=1, question: Optional[str] = None):
    global dffinal
    if question is None:
        raise HTTPException(status_code=400, detail="Question is required")
    if dffinal is None:
        return ["error: CSV has not been loaded or processed yet."]

    for i in range(retries):
        try:
            # save_df(dffinal) # save_df might not be needed here if already set in posttd
            results = td_agent_scratch_pad(question, dffinal)  # Pass dffinal
            return results
        except AttributeError as ex:
            log.exception(f"Exception in /getTdAnalysis: {str(ex)}")
            if i < retries - 1:
                await asyncio.sleep(delay)
            # More specific check
            elif "'NoneType' object has no attribute 'stream'" in str(ex):
                return ["error: CSV related error, potentially not loaded correctly."]
            else:
                raise HTTPException(status_code=500, detail=str(ex)) from ex
        except Exception as ex:
            log.exception(f"Exception in /getTdAnalysis: {str(ex)}")
            if i < retries - 1:
                await asyncio.sleep(delay)
            else:
                raise HTTPException(status_code=500, detail=str(ex)) from ex


@app.post("/refresh")
async def refresh():
    """Refresh the tabular data agent's state."""
    global dffinal
    try:
        refreshagent()
        dffinal = None  # Clear the dataframe on refresh
        log.info("Tabular data agent refreshed.")
    except Exception as ex:
        log.exception("Exception in /refresh")
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    return {"status": "success"}


@app.get("/tdstream")
async def td_stream_response(question: str):
    """Stream the scratchpad output for tabular data analysis."""
    if dffinal is None:
        async def error_stream():
            yield f'event: error\ndata: {json.dumps({"error": "CSV has not been loaded or processed yet."})}\n\n'
            yield f'event: end\ndata: Stream ended\n\n'
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    try:
        # save_df(dffinal) might not be needed if set elsewhere
        stream = td_agent_scratch_pad(question, dffinal)  # Pass dffinal
        return StreamingResponse(stream, media_type="text/event-stream")
    except Exception as ex:
        log.exception("Exception in /tdstream")

        async def error_stream():
            yield f'event: error\ndata: {json.dumps({"error": str(ex)})}\n\n'
            yield f'event: end\ndata: Stream ended\n\n'
        return StreamingResponse(error_stream(), media_type="text/event-stream")
# --- End Tabular Data Assistant Endpoints ---


@app.get("/getFeatureFlags")
async def get_feature_flags():
    """Get feature flag settings."""
    response = {
        "ENABLE_WEB_CHAT": str_to_bool.get(ENV["ENABLE_WEB_CHAT"], False),
        "ENABLE_UNGROUNDED_CHAT": str_to_bool.get(ENV["ENABLE_UNGROUNDED_CHAT"], False),
        "ENABLE_MATH_ASSISTANT": str_to_bool.get(ENV["ENABLE_MATH_ASSISTANT"], False),
        "ENABLE_TABULAR_DATA_ASSISTANT": str_to_bool.get(ENV["ENABLE_TABULAR_DATA_ASSISTANT"], False),
        "ENABLE_MULTIMEDIA": str_to_bool.get(ENV["ENABLE_MULTIMEDIA"], False),
    }
    return response

# This endpoint is missing in the user's 745-line version


@app.post("/file")
async def upload_file(
    file: UploadFile = File(...),
    file_path: str = Form(...),  # Changed from Optional[str] to str
    tags: Optional[str] = Form(None)  # Tags remain optional
):
    """
    Upload a file to Azure Blob Storage.
    Parameters:
    - file: The file to upload.
    - file_path: The path to save the file in Blob Storage (including filename). Required.
    - tags: The tags to associate with the file (comma-separated string).
    Returns:
    - response: A message indicating the result of the upload.
    """
    if not file_path:
        raise HTTPException(
            status_code=400, detail="file_path form data is required.")

    try:
        # Prepare metadata only if tags are provided
        metadata = {"tags": tags} if tags else None

        blob_upload_client = blob_upload_container_client.get_blob_client(
            file_path)

        # Use upload_blob for potentially large files, handles chunking automatically
        blob_upload_client.upload_blob(
            file.file,  # Pass the file-like object directly
            overwrite=True,
            content_settings=ContentSettings(content_type=file.content_type),
            metadata=metadata  # Pass metadata dict or None
        )

        return {"message": f"File '{file.filename}' uploaded successfully to '{file_path}'"}

    except Exception as ex:
        log.exception(f"Exception in /file endpoint for path: {file_path}")
        raise HTTPException(status_code=500, detail=str(ex)) from ex

# This endpoint is missing in the user's 745-line version


@app.post("/get-file")
async def get_file(request: Request):
    """ Endpoint to fetch citation file content based on path """
    try:
        data = await request.json()
        file_path = data.get('path')
        if not file_path:
            raise HTTPException(
                status_code=400, detail="File path ('path') is required in the request body.")

        # Assuming file_path includes the container name like 'upload/folder/file.pdf'
        container_name, blob_name = file_path.split('/', 1)

        # Decide which container client to use based on the path prefix
        if container_name == ENV["AZURE_BLOB_STORAGE_UPLOAD_CONTAINER"]:
            container_client_to_use = blob_upload_container_client
        elif container_name == ENV["AZURE_BLOB_STORAGE_CONTAINER"]:
            container_client_to_use = blob_container
        else:
            raise HTTPException(
                status_code=400, detail=f"Invalid container specified in path: {container_name}")

        citation_blob_client = container_client_to_use.get_blob_client(
            blob=blob_name)

        if not citation_blob_client.exists():
            raise HTTPException(
                status_code=404, detail=f"File not found at path: {file_path}")

        blob_properties = citation_blob_client.get_blob_properties()
        stream = citation_blob_client.download_blob().chunks()

        return StreamingResponse(stream,
                                 media_type=blob_properties.content_settings.content_type or "application/octet-stream",
                                 headers={"Content-Disposition": f"inline; filename=\"{os.path.basename(blob_name)}\""})
    except HTTPException as http_ex:
        # Re-raise HTTPExceptions directly
        raise http_ex
    except Exception as ex:
        log.exception(
            f"Exception in /get-file endpoint for path: {data.get('path', 'Not Provided')}")
        raise HTTPException(
            status_code=500, detail=f"Internal server error: {str(ex)}")


# Mount the static files directory
# This should be one of the last things added to app
app.mount("/", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    log.info("IA WebApp Starting Up...")
