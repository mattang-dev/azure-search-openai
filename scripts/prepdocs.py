import argparse
import base64
import glob
import html
import io
import os
import re
import pdb
import tempfile
import time
from datetime import datetime, timezone
from PyPDF2 import PdfReader
from transformers import AutoModel, AutoTokenizer
from typing import Set
from pdfreader import PDFDocument, SimplePDFViewer
import torch
import openai
import tiktoken
from langchain.embeddings import HuggingFaceInstructEmbeddings
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from azure.identity import AzureDeveloperCliCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswParameters,
    PrioritizedFields,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticSettings,
    SimpleField,
    VectorSearch,
    VectorSearchAlgorithmConfiguration,
)
from azure.storage.blob import BlobServiceClient
from azure.storage.filedatalake import (
    DataLakeServiceClient,
)
from pypdf import PdfReader, PdfWriter
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

args = argparse.Namespace(
    verbose=False,
    openaihost="azure",
    datalakestorageaccount=None,
    datalakefilesystem=None,
    datalakepath=None,
    remove=False,
    useacls=False,
    skipblobs=False,
    storageaccount=None,
    container=None,
)
adls_gen2_creds = None
storage_creds = None

MAX_SECTION_LENGTH = 1000
SENTENCE_SEARCH_LIMIT = 100
SECTION_OVERLAP = 100

open_ai_token_cache: dict[str, any] = {}
CACHE_KEY_TOKEN_CRED = "openai_token_cred"
CACHE_KEY_CREATED_TIME = "created_time"
CACHE_KEY_TOKEN_TYPE = "token_type"

# Embedding batch support section
SUPPORTED_BATCH_AOAI_MODEL = {"text-embedding-ada-002": {"token_limit": 8100, "max_batch_size": 16}}

def calculate_tokens_emb_aoai(input: str):
    encoding = tiktoken.encoding_for_model(args.openaimodelname)
    return len(encoding.encode(input))

def blob_name_from_file_page(filename, page=0):
    if os.path.splitext(filename)[1].lower() == ".pdf":
        return os.path.splitext(os.path.basename(filename))[0] + f"-{page}" + ".pdf"
    else:
        return os.path.basename(filename)

def upload_blobs(filename):
    blob_service = BlobServiceClient(
        account_url=f"https://{args.storageaccount}.blob.core.windows.net", credential=storage_creds
    )
    blob_container = blob_service.get_container_client(args.container)
    if not blob_container.exists():
        blob_container.create_container()

    def blob_exists(blob_name):
        blob_client = blob_container.get_blob_client(blob_name)
        return blob_client.exists()

    def ask_overwrite(blob_name):
        answer = input(f"The blob {blob_name} already exists. Overwrite? [y/N]: ").strip().lower()
        return answer == 'y'

    # if file is PDF split into pages and upload each page as a separate blob
    if os.path.splitext(filename)[1].lower() == ".pdf":
        reader = PdfReader(filename)
        pages = reader.pages
        for i in range(len(pages)):
            blob_name = blob_name_from_file_page(filename, i)
            if blob_exists(blob_name):
                if args.verbose:
                    print(f"\tBlob for page {i} -> {blob_name} already exists.")
                if not ask_overwrite(blob_name):
                    if args.verbose:
                        print("\tSkipping upload based on user input.")
                    continue  # Skip if user chooses not to overwrite
                else:
                    if args.verbose:
                        print("\tUser chose to overwrite the blob.")
            if args.verbose:
                print(f"\tUploading blob for page {i} -> {blob_name}")
            f = io.BytesIO()
            writer = PdfWriter()
            writer.add_page(pages[i])
            writer.write(f)
            f.seek(0)
            blob_container.upload_blob(blob_name, f, overwrite=True)
    else:
        blob_name = blob_name_from_file_page(filename)
        if blob_exists(blob_name):
            if args.verbose:
                print(f"\tBlob {blob_name} already exists.")
            if not ask_overwrite(blob_name):
                if args.verbose:
                    print("\tSkipping upload based on user input.")
                return  # Exit the function if user chooses not to overwrite
            else:
                if args.verbose:
                    print("\tUser chose to overwrite the blob.")
        with open(filename, "rb") as data:
            blob_container.upload_blob(blob_name, data, overwrite=True)

def remove_blobs(filename):
    if args.verbose:
        print(f"Removing blobs for '{filename or '<all>'}'")
    blob_service = BlobServiceClient(
        account_url=f"https://{args.storageaccount}.blob.core.windows.net", credential=storage_creds
    )
    blob_container = blob_service.get_container_client(args.container)
    if blob_container.exists():
        if filename is None:
            blobs = blob_container.list_blob_names()
        else:
            prefix = os.path.splitext(os.path.basename(filename))[0]
            blobs = filter(
                lambda b: re.match(f"{prefix}-\d+\.pdf", b),
                blob_container.list_blob_names(name_starts_with=os.path.splitext(os.path.basename(prefix))[0]),
            )
        for b in blobs:
            if args.verbose:
                print(f"\tRemoving blob {b}")
            blob_container.delete_blob(b)

def table_to_html(table):
    table_html = "<table>"
    rows = [
        sorted([cell for cell in table.cells if cell.row_index == i], key=lambda cell: cell.column_index)
        for i in range(table.row_count)
    ]
    for row_cells in rows:
        table_html += "<tr>"
        for cell in row_cells:
            tag = "th" if (cell.kind == "columnHeader" or cell.kind == "rowHeader") else "td"
            cell_spans = ""
            if cell.column_span > 1:
                cell_spans += f" colSpan={cell.column_span}"
            if cell.row_span > 1:
                cell_spans += f" rowSpan={cell.row_span}"
            table_html += f"<{tag}{cell_spans}>{html.escape(cell.content)}</{tag}>"
        table_html += "</tr>"
    table_html += "</table>"
    return table_html

def get_document_text(filename):
    offset = 0
    page_map = []
    if args.localpdfparser:
        reader = PdfReader(filename)
        pages = reader.pages
        for page_num, p in enumerate(pages):
            page_text = p.extract_text()
            page_map.append((page_num, offset, page_text))
            offset += len(page_text)
    else:
        if args.verbose:
            print(f"Extracting text from '{filename}' using Azure Form Recognizer")
        form_recognizer_client = DocumentAnalysisClient(
            endpoint=f"https://{args.formrecognizerservice}.cognitiveservices.azure.com/",
            credential=formrecognizer_creds,
            headers={"x-ms-useragent": "azure-search-chat-demo/1.0.0"},
        )
        with open(filename, "rb") as f:
            poller = form_recognizer_client.begin_analyze_document("prebuilt-layout", document=f)
        form_recognizer_results = poller.result()

        for page_num, page in enumerate(form_recognizer_results.pages):
            tables_on_page = [
                table
                for table in (form_recognizer_results.tables or [])
                if table.bounding_regions and table.bounding_regions[0].page_number == page_num + 1
            ]

            # mark all positions of the table spans in the page
            page_offset = page.spans[0].offset
            page_length = page.spans[0].length
            table_chars = [-1] * page_length
            for table_id, table in enumerate(tables_on_page):
                for span in table.spans:
                    # replace all table spans with "table_id" in table_chars array
                    for i in range(span.length):
                        idx = span.offset - page_offset + i
                        if idx >= 0 and idx < page_length:
                            table_chars[idx] = table_id

            # build page text by replacing characters in table spans with table html
            page_text = ""
            added_tables = set()
            for idx, table_id in enumerate(table_chars):
                if table_id == -1:
                    page_text += form_recognizer_results.content[page_offset + idx]
                elif table_id not in added_tables:
                    page_text += table_to_html(tables_on_page[table_id])
                    added_tables.add(table_id)

            page_text += " "
            page_map.append((page_num, offset, page_text))
            offset += len(page_text)

    return page_map

def extract_first_page_text_local(filename):
    """
    Extracts text from the first page of the given PDF document using a local PDF parser.

    Parameters:
    filename (str): The path to the PDF document file.

    Returns:
    str: The text of the first page.
    """
    reader = PdfReader(filename)
    first_page = reader.pages[0]  # Get the first page
    page_text = first_page.extract_text()  # Extract text from the first page
    print (f'Text from the first page:{page_text}')
    return page_text

def split_text(page_map, filename):
    SENTENCE_ENDINGS = [".", "!", "?"]
    WORDS_BREAKS = [",", ";", ":", " ", "(", ")", "[", "]", "{", "}", "\t", "\n"]
    if args.verbose:
        print(f"Splitting '{filename}' into sections")

    def find_page(offset):
        num_pages = len(page_map)
        for i in range(num_pages - 1):
            if offset >= page_map[i][1] and offset < page_map[i + 1][1]:
                return i
        return num_pages - 1

    all_text = "".join(p[2] for p in page_map)
    length = len(all_text)
    start = 0
    end = length
    while start + SECTION_OVERLAP < length:
        last_word = -1
        end = start + MAX_SECTION_LENGTH

        if end > length:
            end = length
        else:
            # Try to find the end of the sentence
            while (
                end < length
                and (end - start - MAX_SECTION_LENGTH) < SENTENCE_SEARCH_LIMIT
                and all_text[end] not in SENTENCE_ENDINGS
            ):
                if all_text[end] in WORDS_BREAKS:
                    last_word = end
                end += 1
            if end < length and all_text[end] not in SENTENCE_ENDINGS and last_word > 0:
                end = last_word  # Fall back to at least keeping a whole word
        if end < length:
            end += 1

        # Try to find the start of the sentence or at least a whole word boundary
        last_word = -1
        while (
            start > 0
            and start > end - MAX_SECTION_LENGTH - 2 * SENTENCE_SEARCH_LIMIT
            and all_text[start] not in SENTENCE_ENDINGS
        ):
            if all_text[start] in WORDS_BREAKS:
                last_word = start
            start -= 1
        if all_text[start] not in SENTENCE_ENDINGS and last_word > 0:
            start = last_word
        if start > 0:
            start += 1

        section_text = all_text[start:end]
        yield (section_text, find_page(start))

        last_table_start = section_text.rfind("<table")
        if last_table_start > 2 * SENTENCE_SEARCH_LIMIT and last_table_start > section_text.rfind("</table"):
            # If the section ends with an unclosed table, we need to start the next section with the table.
            # If table starts inside SENTENCE_SEARCH_LIMIT, we ignore it, as that will cause an infinite loop for tables longer than MAX_SECTION_LENGTH
            # If last table starts inside SECTION_OVERLAP, keep overlapping
            if args.verbose:
                print(
                    f"Section ends with unclosed table, starting next section with the table at page {find_page(start)} offset {start} table start {last_table_start}"
                )
            start = min(end - SECTION_OVERLAP, start + last_table_start)
        else:
            start = end - SECTION_OVERLAP

    if start + SECTION_OVERLAP < end:
        yield (all_text[start:end], find_page(start))
        
        # section_text = all_text[start:end]
        # 
        # yield (section_text, find_page(start))

    #     last_table_start = section_text.rfind("<table")
    #     if last_table_start > 2 * SENTENCE_SEARCH_LIMIT and last_table_start > section_text.rfind("</table"):
    #         # If the section ends with an unclosed table, we need to start the next section with the table.
    #         # If table starts inside SENTENCE_SEARCH_LIMIT, we ignore it, as that will cause an infinite loop for tables longer than MAX_SECTION_LENGTH
    #         # If last table starts inside SECTION_OVERLAP, keep overlapping
    #         if args.verbose:
    #             print(
    #                 f"Section ends with unclosed table, starting next section with the table at page {find_page(start)} offset {start} table start {last_table_start}"
    #             )
    #         start = min(end - SECTION_OVERLAP, start + last_table_start)
    #     else:
    #         start = end - SECTION_OVERLAP

    # if start + SECTION_OVERLAP < end:
    #     section_text = all_text[start:end]
    #     print(f"Final section from {start} to {end} on page {find_page(start)}: {section_text}")
    #     yield (all_text[start:end], find_page(start))


def filename_to_id(filename):
    filename_ascii = re.sub("[^0-9a-zA-Z_-]", "_", filename)
    filename_hash = base64.b16encode(filename.encode("utf-8")).decode("ascii")
    return f"file-{filename_ascii}-{filename_hash}"

def get_title(filename):
    with open(filename, 'rb') as file:
        pdf = PdfReader(file)
        title = pdf.metadata.title  # Change this line
        if not title:
            title = os.path.basename(filename)
    
    user_title = input(f"Press Enter to use title: '{title}' for '{filename}' or enter a new title for the file:\n").strip()
    if user_title:
        title = user_title
    
    return title

def get_url(filename):
    user_url = input("Please provide a URL for the file (or press Enter to use the blob URL): ").strip()
    if user_url:
        return user_url

    # rest of your code to get the blob URL...
    blob_service = BlobServiceClient(
        account_url=f"https://{args.storageaccount}.blob.core.windows.net", credential=storage_creds
    )
    blob_container = blob_service.get_container_client(args.container)
    url = f"https://{args.storageaccount}.blob.core.windows.net/{args.container}/{filename}"
    print(f"The blob storage URL is: {url}")
    return url

def get_unique_categories(index_name: str) -> str:
    """
    Retrieves all unique categories from an Azure Cognitive Search index.

    Parameters:
    index_name (str): The name of the index in the search service.

    Returns:
    str: A string of unique categories found in the index, separated by commas.
    """
    search_client = SearchClient(endpoint=f"https://{args.searchservice}.search.windows.net/",
                                 index_name=index_name,
                                 credential=search_creds)

    try:
        results = search_client.search(search_text="", select="category")

        unique_categories = set()
        for result in results.by_page():
            for doc in result:
                categories = doc['category'].split(',')
                unique_categories.update(map(str.strip, categories))  # Strip whitespace from each category

        categories = ', '.join(unique_categories)
        return categories # Return a string

    except Exception as e:
        # Handle or log the error appropriately
        print(f"An error occurred while fetching categories: {e}")
        return ""


def get_first_page_paragraphs(filename):
    """
    Extracts the first two paragraphs from the first page of the given PDF document.

    Parameters:
    filename (str): The path to the PDF document file.

    Returns:
    List[str]: A list containing the first two paragraphs of the first page.
    """
    # Use the previously defined function to extract text from the first page
    first_page_text = extract_first_page_text_local(filename)

    # Split the text into paragraphs. This assumes that paragraphs are separated by two newline characters
    paragraphs = first_page_text.split('\n\n')

    # Select the first two paragraphs
    first_two_paragraphs = paragraphs[:2]

    return first_two_paragraphs

def get_category(index_categories, filename,):
    if args.category:
        return args.category
    
    text = "\n\n".join(get_first_page_paragraphs(filename))  # Combine paragraphs into a string

    try:
        openai.api_type = "azure"
        openai.api_base = "https://canadianai.openai.azure.com/"
        openai.api_version = "2023-07-01-preview"
        openai.api_key = "c01fead469fe45b48917fbdb6e7fb7c3"
        query_engine= "gpt4"
        filename=(f'{filename}')
        response = openai.ChatCompletion.create(
        engine=query_engine,
        messages=[
                {
                    "role": "system",
                    "content": f'You are a Library AI assistant that helps categorize documents. '
                               f'Review the following category list and decide where the document should be categorized:'
                               f'\n{index_categories}\n'
                },
                {
                    "role": "user",
                    "content": f'The document titled "{filename}" begins with: {text} '
                               f'What are 5 categories that best describe it? You must only respond with a single string of 5 categories. Do not reply with anything other than a single string.'
                }
            ],
            temperature=0.1,
            max_tokens=800,
            top_p=0.2,
            frequency_penalty=1,
            presence_penalty=1,
        )
        ai_response = response.choices[0].message.content
        categories = ai_response.split(',')
        selected_category = input(f'Index Current {index_categories} \n\nGPT4 has chosen: {ai_response}\n\nDo you want to use GPT4\'s categories? Enter ([Yes]/No):').strip().lower()
        if selected_category in ['no', 'n']:
            categories = []
            for i in range(5):
                category = input(f"Category[{i+1}/5]: ").strip()
                if category:  # check if the input is not empty
                    categories.append(category)
            # join the categories into a single string
            categories_string = ','.join(categories)
            print(f"Selected categories: {categories_string}")
            return categories_string
        else:
            print(f'Selected categories: {ai_response}')
            return ','.join(categories).strip()
    except openai.error.OpenAIError as e:
        print(f"An error occurred: {e}. Error: Unable to retrieve categories.")
        categories = []
        for i in range(5):
            category = input(f"Category[{i+1}/5]: ").strip()
            if category:  # check if the input is not empty
                categories.append(category)
            # join the categories into a single string
            categories_string = ','.join(categories)
            print(f"Selected categories: {categories_string}")
        return categories_string

def create_sections(filename, page_map, use_vectors, embedding_deployment: str = None, embedding_model: str = None):

    # Get the current time in UTC
    now = datetime.now(timezone.utc)

    # Format the timestamp to include date, time, and timezone offset (Z indicates UTC)
    timestamp = now.isoformat(timespec='milliseconds')
    file_id = filename_to_id(os.path.basename(filename))
    title = get_title(filename)
    url = get_url(filename)
    index_categories = get_unique_categories(args.index,)
    category = get_category(index_categories, filename)

    for i, (content, pagenum) in enumerate(split_text(page_map, filename)):
        section = {
            "id": f"{file_id}-page-{i}",
            "content": content,
            "category": category,
            "sourcepage": blob_name_from_file_page(filename, pagenum),
            "sourcefile": filename,
            "timestamp": timestamp,
            "title": title,
            "url": url
            
        }
        page_number=i
        if args.localvectors:
            section["emnbeding"] = local_compute_embedding(content, page_number)
            print(f'Page {page_number} vectorized')
        elif use_vectors:
            section["embedding"] = compute_embedding(content, embedding_deployment, embedding_model, page_number)
            print(f'Page {page_number} vectorized')
            
        yield section


def before_retry_sleep(retry_state):
    if args.verbose:
        print("Rate limited on the OpenAI embeddings API, sleeping before retrying...")

# Create embeddings
model_name = "sentence-transformers/all-MiniLM-L6-v2"
# model_name = "sentence-transformers/msmarco-distilroberta-base-v2"
model = AutoModel.from_pretrained(model_name) 
tokenizer = AutoTokenizer.from_pretrained(model_name)

def local_compute_embedding(text, page_number):
    print(f"Local Embeddings\nInput text: {text}")
    inputs = tokenizer(text, padding=True, return_tensors="pt")
    print(f"Embedding inputs: {inputs}")
    outputs = model(**inputs)
    embeddings = outputs.last_hidden_state[:, 0, :]
    embeddings = torch.nn.functional.normalize(embeddings)
    # Write the embedding to a file for easier checking
    with open(f'embedding_{page_number}.txt', encoding='utf-8', mode='w',errors='ignore') as f:
            f.write(str(embeddings))
    return embeddings.detach().numpy()

@retry(
    retry=retry_if_exception_type(openai.error.RateLimitError),
    wait=wait_random_exponential(min=15, max=60),
    stop=stop_after_attempt(15),
    before_sleep=before_retry_sleep,
)
def compute_embedding(text, embedding_deployment, embedding_model):
    refresh_openai_token()
    embedding_args = {"deployment_id": embedding_deployment} if args.openaihost != "openai" else {}
    return openai.Embedding.create(**embedding_args, model=embedding_model, input=text)["data"][0]["embedding"]

@retry(
    retry=retry_if_exception_type(openai.error.RateLimitError),
    wait=wait_random_exponential(min=15, max=60),
    stop=stop_after_attempt(15),
    before_sleep=before_retry_sleep,
)
def compute_embedding_in_batch(texts):
    refresh_openai_token()
    embedding_args = {"deployment_id": args.openaideployment} if args.openaihost != "openai" else {}
    emb_response = openai.Embedding.create(**embedding_args, model=args.openaimodelname, input=texts)
    return [data.embedding for data in emb_response.data]

def local_compute_embeddings_in_batch(texts, page_number):
  inputs = tokenizer(texts, padding=True, return_tensors="pt")
  outputs = model(**inputs)
  # Get the embeddings from the last layer
  embeddings = outputs.last_hidden_state[:, 0, :]
  # Normalize the embeddings  
  embeddings = torch.nn.functional.normalize(embeddings)
 # Write the embedding to a file for easier checking
  with open(f'embedding_{page_number}.txt', 'w') as f:
            f.write(str(embeddings))
  return embeddings.detach().numpy()

def create_search_index():
    if args.verbose:
        print(f"Ensuring search index {args.index} exists")
    index_client = SearchIndexClient(
        endpoint=f"https://{args.searchservice}.search.windows.net/", credential=search_creds
    )
    fields = [
        SimpleField(name="id", type="Edm.String", key=True),
        SearchableField(name="content", type="Edm.String", analyzer_name="en.microsoft"),
        SearchField(
            name="embedding",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            hidden=False,
            searchable=True,
            filterable=False,
            sortable=False,
            facetable=False,
            vector_search_dimensions=1536,
            vector_search_configuration="default",
        ),
        SimpleField(name="category", type="Edm.String", filterable=True, facetable=True),
        SimpleField(name="sourcepage", type="Edm.String", filterable=True, facetable=True),
        SimpleField(name="sourcefile", type="Edm.String", filterable=True, facetable=True),
    ]
    if args.useacls:
        fields.append(
            SimpleField(name="oids", type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True)
        )
        fields.append(
            SimpleField(name="groups", type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True)
        )

    if args.index not in index_client.list_index_names():
        index = SearchIndex(
            name=args.index,
            fields=fields,
            semantic_settings=SemanticSettings(
                configurations=[
                    SemanticConfiguration(
                        name="default",
                        prioritized_fields=PrioritizedFields(
                            title_field=None, prioritized_content_fields=[SemanticField(field_name="content")]
                        ),
                    )
                ]
            ),
            vector_search=VectorSearch(
                algorithm_configurations=[
                    VectorSearchAlgorithmConfiguration(
                        name="default", kind="hnsw", hnsw_parameters=HnswParameters(metric="cosine")
                    )
                ]
            ),
        )
        if args.verbose:
            print(f"Creating {args.index} search index")
        index_client.create_index(index)
    else:
        if args.verbose:
            print(f"Search index {args.index} already exists")
            
def update_embeddings_in_batch(sections):
    batch_queue: list = []
    copy_s = []
    batch_response = {}
    token_count = 0
    for s in sections:
        token_count += calculate_tokens_emb_aoai(s["content"])
        if (
            token_count <= SUPPORTED_BATCH_AOAI_MODEL[args.openaimodelname]["token_limit"]
            and len(batch_queue) < SUPPORTED_BATCH_AOAI_MODEL[args.openaimodelname]["max_batch_size"]
        ):
            batch_queue.append(s)
            copy_s.append(s)
        else:
            emb_responses = compute_embedding_in_batch([item["content"] for item in batch_queue])
            if args.verbose:
                print(f"Batch Completed. Batch size  {len(batch_queue)} Token count {token_count}")
            for emb, item in zip(emb_responses, batch_queue):
                batch_response[item["id"]] = emb
            batch_queue = []
            batch_queue.append(s)
            token_count = calculate_tokens_emb_aoai(s["content"])

    if batch_queue:
        emb_responses = compute_embedding_in_batch([item["content"] for item in batch_queue])
        if args.verbose:
            print(f"Batch Completed. Batch size  {len(batch_queue)} Token count {token_count}")
        for emb, item in zip(emb_responses, batch_queue):
            batch_response[item["id"]] = emb

    for s in copy_s:
        s["embedding"] = batch_response[s["id"]]
        yield s


def index_sections(filename, sections, acls=None):
    if args.verbose:
        print(f"Indexing sections from '{filename}' into search index '{args.index}'")
    search_client = SearchClient(
        endpoint=f"https://{args.searchservice}.search.windows.net/", index_name=args.index, credential=search_creds
    )
    i = 0
    batch = []
    for s in sections:
        if acls:
            s.update(acls)
        batch.append(s)
        i += 1
        if i % 1000 == 0:
            results = search_client.upload_documents(documents=batch)
            succeeded = sum([1 for r in results if r.succeeded])
            if args.verbose:
                print(f"\tIndexed {len(results)} sections, {succeeded} succeeded")
            batch = []

    if len(batch) > 0:
        results = search_client.upload_documents(documents=batch)
        succeeded = sum([1 for r in results if r.succeeded])
        if args.verbose:
            print(f"\tIndexed {len(results)} sections, {succeeded} succeeded")
            print (f'Document Indexed sueccessfully!')

def remove_from_index(filename):
    if args.verbose:
        print(f"Removing sections from '{filename or '<all>'}' from search index '{args.index}'")
    search_client = SearchClient(
        endpoint=f"https://{args.searchservice}.search.windows.net/", index_name=args.index, credential=search_creds
    )
    while True:
        filter = None if filename is None else f"sourcefile eq '{os.path.basename(filename)}'"
        r = search_client.search("", filter=filter, top=1000, include_total_count=True)
        if r.get_count() == 0:
            break
        r = search_client.delete_documents(documents=[{"id": d["id"]} for d in r])
        if args.verbose:
            print(f"\tRemoved {len(r)} sections from index")
        # It can take a few seconds for search results to reflect changes, so wait a bit
        time.sleep(2)

def refresh_openai_token():
    """
    Refresh OpenAI token every 5 minutes
    """
    if (
        CACHE_KEY_TOKEN_TYPE in open_ai_token_cache
        and open_ai_token_cache[CACHE_KEY_TOKEN_TYPE] == "azure_ad"
        and open_ai_token_cache[CACHE_KEY_CREATED_TIME] + 300 < time.time()
    ):
        token_cred = open_ai_token_cache[CACHE_KEY_TOKEN_CRED]
        openai.api_key = token_cred.get_token("https://cognitiveservices.azure.com/.default").token
        open_ai_token_cache[CACHE_KEY_CREATED_TIME] = time.time()

def is_file_indexed(filename):
    search_client = SearchClient(
        endpoint=f"https://{args.searchservice}.search.windows.net/", index_name=args.index, credential=search_creds
    )
    # Formulate the search query
    search_query = f"sourcefile eq '{os.path.basename(filename)}'&$count=true"
    
    # Execute the search query
    results = search_client.search(search_text="", filter=search_query, include_total_count=True)
    
    # Get the total count of matching documents
    total_count = results.get_count()
    
    print(f"Total count of documents matching '{os.path.basename(filename)}': {total_count}")
    
    return total_count > 0

def remove_duplicates(search_client):
    # Get all documents in the index
    results = search_client.search(search_text="*")

    # Group documents by sourcefile and sourcepage
    documents_by_sourcefile_and_sourcepage = {}
    for result in results:
        sourcefile = result["sourcefile"]
        sourcepage = result["sourcepage"]
        if (sourcefile, sourcepage) not in documents_by_sourcefile_and_sourcepage:
            documents_by_sourcefile_and_sourcepage[(sourcefile, sourcepage)] = [result]
        else:
            # If timestamp is not available, keep only the first document and remove duplicates
            if "timestamp" not in result or "timestamp" not in documents_by_sourcefile_and_sourcepage[(sourcefile, sourcepage)][0]:
                continue
            documents_by_sourcefile_and_sourcepage[(sourcefile, sourcepage)].append(result)

    # For each sourcefile and sourcepage, sort the documents by timestamp if available
    documents_to_delete = []
    for (sourcefile, sourcepage), documents in documents_by_sourcefile_and_sourcepage.items():
        # Sort documents by timestamp if available
        if "timestamp" in documents[0]:
            documents.sort(key=lambda doc: doc["timestamp"] if doc["timestamp"] is not None else datetime.min, reverse=True)
        
        # Add all but the most recent document to the list of documents to delete
        for document in documents[1:]:
            documents_to_delete.append({"@search.action": "delete", "id": document["id"]})

    # If there are no duplicates, print a message and exit
    if len(documents_to_delete) == 0:
        print(f"There are no duplicates for {sourcefile}")
        return

    # Print the IDs of the duplicate documents
    print("Duplicate document IDs:")
    for document in documents_to_delete:
        print(document["id"])

    # Print the number of documents to delete and ask for confirmation
    print(f"\nFound {len(documents_to_delete)} duplicate documents.")
    confirmation = input("Do you want to delete these documents? (yes/no) ")
    if confirmation.lower() == "yes":
        # Delete the documents
        search_client.delete_documents(documents_to_delete)

def get_page_count(filename):
    # Open the file in read-binary mode
    with open(filename, 'rb') as file:
        # Create a PDF file reader object
        pdf = PdfReader(file)
        # Get the number of pages in the PDF
        total_pages = len(pdf.pages)

    return total_pages

def are_all_pages_indexed(filename, total_pages):
    search_client = SearchClient(
        endpoint=f"https://{args.searchservice}.search.windows.net/", index_name=args.index, credential=search_creds
    )
    # Formulate the search query
    search_query = f"sourcefile eq '{os.path.basename(filename)}'"

    # Execute the search query
    results = search_client.search(search_text="", filter=search_query, include_total_count=True)

    # Get the total count of matching documents
    indexed_pages = results.get_count()
    
    print(f"Total pages: {total_pages}")
    print(f"Indexed pages: {indexed_pages}")

    return indexed_pages == total_pages + 1  # Account for "page 0"

def get_unindexed_pages(filename, total_pages):
    search_client = SearchClient(
        endpoint=f"https://{args.searchservice}.search.windows.net/", index_name=args.index, credential=search_creds
    )
    # Formulate the search query
    search_query = f"sourcefile eq '{os.path.basename(filename)}'"

    # Execute the search query
    results = search_client.search(search_text="", filter=search_query, include_total_count=True)

    # Get a list of all indexed page numbers
    indexed_page_numbers = [int(result["sourcepage"].split('-')[-1].split('.')[0]) for result in results]

    # Then, get a list of all page numbers
    all_page_numbers = list(range(0, total_pages))  # Pages start from 0

    # Finally, return a list of page numbers that are not in indexed_page_numbers
    unindexed_pages = [page for page in all_page_numbers if page not in indexed_page_numbers]
    
    print(f"The following pages for file {filename} have not been indexed: {unindexed_pages}")

    return unindexed_pages

def read_files(
    path_pattern: str,
    use_vectors: bool,
    vectors_batch_support: bool,
    embedding_deployment: str,
    embedding_model: str,
):
    # Create a search client
    search_client = SearchClient(
        endpoint=f"https://{args.searchservice}.search.windows.net/", index_name=args.index, credential=search_creds
    )
    for filename in glob.glob(path_pattern):
        if args.verbose:
            print(f"Processing '{filename}'")
        
        if args.remove:
            remove_blobs(filename)
            remove_from_index(filename)
        else:
            total_pages = get_page_count(filename)
            unindexed_pages = get_unindexed_pages(filename, total_pages)
            if not unindexed_pages:
                print(f"All pages from {filename} are already indexed, skipping.")
                 # Remove any duplicate documents for the file
                remove_duplicates(search_client)
                continue
            elif os.path.isdir(filename):
                # Recursively read subdirectories
                read_files(filename + "/*", use_vectors, vectors_batch_support)
                continue
            try:
                if not args.skipblobs:
                    upload_blobs(filename)
                # Get the document text for the file
                page_map = get_document_text(filename)
                # Create sections for the file
                sections = create_sections(
                    filename,
                    page_map,
                    use_vectors and not vectors_batch_support,                   
                )
                # Update embeddings in batch if available
                if use_vectors and vectors_batch_support:
                    sections = update_embeddings_in_batch(sections)

                index_sections(os.path.basename(filename), sections)
            except Exception as e:
                print(f"\tGot an error while reading {filename} -> {e} --> skipping file")

def read_adls_gen2_files(
    use_vectors: bool, vectors_batch_support: bool, embedding_deployment: str = None, embedding_model: str = None
):
    datalake_service = DataLakeServiceClient(
        account_url=f"https://{args.datalakestorageaccount}.dfs.core.windows.net", credential=adls_gen2_creds
    )
    filesystem_client = datalake_service.get_file_system_client(file_system=args.datalakefilesystem)
    paths = filesystem_client.get_paths(path=args.datalakepath, recursive=True)
    for path in paths:
        if not path.is_directory:
            if args.remove:
                remove_blobs(path.name)
                remove_from_index(path.name)
            else:
                temp_file_path = os.path.join(tempfile.gettempdir(), os.path.basename(path.name))
                try:
                    temp_file = open(temp_file_path, "wb")
                    file_client = filesystem_client.get_file_client(path)
                    file_client.download_file().readinto(temp_file)

                    acls = None
                    if args.useacls:
                        # Parse out user ids and group ids
                        acls = {"oids": [], "groups": []}
                        # https://learn.microsoft.com/python/api/azure-storage-file-datalake/azure.storage.filedatalake.datalakefileclient?view=azure-python#azure-storage-filedatalake-datalakefileclient-get-access-control
                        # Request ACLs as GUIDs
                        acl_list = file_client.get_access_control(upn=False)["acl"]
                        # https://learn.microsoft.com/azure/storage/blobs/data-lake-storage-access-control
                        # ACL Format: user::rwx,group::r-x,other::r--,user:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:r--
                        acl_list = acl_list.split(",")
                        for acl in acl_list:
                            acl_parts = acl.split(":")
                            if len(acl_parts) != 3:
                                continue
                            if len(acl_parts[1]) == 0:
                                continue
                            if acl_parts[0] == "user" and "r" in acl_parts[2]:
                                acls["oids"].append(acl_parts[1])
                            if acl_parts[0] == "group" and "r" in acl_parts[2]:
                                acls["groups"].append(acl_parts[1])

                    if not args.skipblobs:
                        upload_blobs(temp_file.name)
                    page_map = get_document_text(temp_file.name)
                    sections = create_sections(
                        os.path.basename(path.name),
                        page_map,
                        use_vectors and not vectors_batch_support,
                        embedding_deployment,
                        embedding_model,
                    )
                    if use_vectors and vectors_batch_support:
                        sections = update_embeddings_in_batch(sections)
                    index_sections(os.path.basename(path.name), sections, acls)
                except Exception as e:
                    print(f"\tGot an error while reading {path.name} -> {e} --> skipping file")
                finally:
                    try:
                        temp_file.close()
                        os.remove(temp_file_path)
                    except Exception as e:
                        print(f"\tGot an error while deleting {temp_file_path} -> {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Prepare documents by extracting content from PDFs, splitting content into sections, uploading to blob storage, and indexing in a search index.",
        epilog="Example: prepdocs.py '..\data\*' --storageaccount myaccount --container mycontainer --searchservice mysearch --index myindex -v",
    )
    parser.add_argument("files", nargs="?", help="Files to be processed")
    parser.add_argument(
        "--datalakestorageaccount", required=False, help="Optional. Azure Data Lake Storage Gen2 Account name"
    )
    parser.add_argument(
        "--datalakefilesystem",
        required=False,
        default="gptkbcontainer",
        help="Optional. Azure Data Lake Storage Gen2 filesystem name",
    )
    parser.add_argument(
        "--localvectors",
        required=False,
        action='store_true',
        help="Use local vectorization and embedding model",
    )
    parser.add_argument(
        "--datalakepath",
        required=False,
        help="Optional. Azure Data Lake Storage Gen2 filesystem path containing files to index. If omitted, index the entire filesystem",
    )
    parser.add_argument(
        "--datalakekey", required=False, help="Optional. Use this key when authenticating to Azure Data Lake Gen2"
    )
    parser.add_argument(
        "--useacls", action="store_true", help="Store ACLs from Azure Data Lake Gen2 Filesystem in the search index"
    )
    parser.add_argument(
        "--category", help="Value for the category field in the search index for all sections indexed in this run"
    )
    parser.add_argument(
        "--skipblobs", action="store_true", help="Skip uploading individual pages to Azure Blob Storage"
    )
    parser.add_argument("--storageaccount", help="Azure Blob Storage account name")
    parser.add_argument("--container", help="Azure Blob Storage container name")
    parser.add_argument(
        "--storagekey",
        required=False,
        help="Optional. Use this Azure Blob Storage account key instead of the current user identity to login (use az login to set current user for Azure)",
    )
    parser.add_argument(
        "--tenantid", required=False, help="Optional. Use this to define the Azure directory where to authenticate)"
    )
    parser.add_argument(
        "--searchservice",
        help="Name of the Azure Cognitive Search service where content should be indexed (must exist already)",
    )
    parser.add_argument(
        "--index",
        help="Name of the Azure Cognitive Search index where content should be indexed (will be created if it doesn't exist)",
    )
    parser.add_argument(
        "--searchkey",
        required=False,
        help="Optional. Use this Azure Cognitive Search account key instead of the current user identity to login (use az login to set current user for Azure)",
    )
    parser.add_argument("--openaihost", help="Host of the API used to compute embeddings ('azure' or 'openai')")
    parser.add_argument("--openaiservice", help="Name of the Azure OpenAI service used to compute embeddings")
    parser.add_argument(
        "--openaideployment",
        help="Name of the Azure OpenAI model deployment for an embedding model ('text-embedding-ada-002' recommended)",
    )
    parser.add_argument(
        "--openaimodelname", help="Name of the Azure OpenAI embedding model ('text-embedding-ada-002' recommended)"
    )
    parser.add_argument(
        "--novectors",
        action="store_true",
        help="Don't compute embeddings for the sections (e.g. don't call the OpenAI embeddings API during indexing)",
    )
    parser.add_argument(
        "--disablebatchvectors", action="store_true", help="Don't compute embeddings in batch for the sections"
    )
    parser.add_argument(
        "--openaikey",
        required=False,
        help="Optional. Use this Azure OpenAI account key instead of the current user identity to login (use az login to set current user for Azure). This is required only when using non-Azure endpoints.",
    )
    parser.add_argument("--openaiorg", required=False, help="This is required only when using non-Azure endpoints.")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove references to this document from blob storage and the search index",
    )
    parser.add_argument(
        "--removeall",
        action="store_true",
        help="Remove all blobs from blob storage and documents from the search index",
    )
    parser.add_argument(
        "--localpdfparser",
        action="store_true",
        help="Use PyPdf local PDF parser (supports only digital PDFs) instead of Azure Form Recognizer service to extract text, tables and layout from the documents",
    )
    parser.add_argument(
        "--formrecognizerservice",
        required=False,
        help="Optional. Name of the Azure Form Recognizer service which will be used to extract text, tables and layout from the documents (must exist already)",
    )
    parser.add_argument(
        "--formrecognizerkey",
        required=False,
        help="Optional. Use this Azure Form Recognizer account key instead of the current user identity to login (use az login to set current user for Azure)",
    )

    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    # Use the current user identity to connect to Azure services unless a key is explicitly set for any of them
    azd_credential = (
        AzureDeveloperCliCredential()
        if args.tenantid is None
        else AzureDeveloperCliCredential(tenant_id=args.tenantid, process_timeout=60)
    )
    default_creds = azd_credential if args.searchkey is None or args.storagekey is None else None
    adls_gen2_creds = azd_credential if args.datalakekey is None else AzureKeyCredential(args.datalakekey)
    search_creds = default_creds if args.searchkey is None else AzureKeyCredential(args.searchkey)
    use_vectors = not args.novectors
    compute_vectors_in_batch = not args.disablebatchvectors and args.openaimodelname in SUPPORTED_BATCH_AOAI_MODEL

    if not args.skipblobs:
        storage_creds = default_creds if args.storagekey is None else args.storagekey
    if not args.localpdfparser:
        # check if Azure Form Recognizer credentials are provided
        if args.formrecognizerservice is None:
            print(
                "Error: Azure Form Recognizer service is not provided. Please provide formrecognizerservice or use --localpdfparser for local pypdf parser."
            )
            exit(1)
        formrecognizer_creds = (
            default_creds if args.formrecognizerkey is None else AzureKeyCredential(args.formrecognizerkey)
        )

    if use_vectors:
        if args.openaihost != "openai":
            if not args.openaikey:
                openai.api_key = azd_credential.get_token("https://cognitiveservices.azure.com/.default").token
                openai.api_type = "azure_ad"
                open_ai_token_cache[CACHE_KEY_CREATED_TIME] = time.time()
                open_ai_token_cache[CACHE_KEY_TOKEN_CRED] = azd_credential
                open_ai_token_cache[CACHE_KEY_TOKEN_TYPE] = "azure_ad"
            else:
                openai.api_key = args.openaikey
                openai.api_type = "azure"
            openai.api_base = f"https://{args.openaiservice}.openai.azure.com"
            openai.api_version = "2023-07-01-preview"
        else:
            print("using normal openai")
            openai.api_key = args.openaikey
            openai.organization = args.openaiorg
            openai.api_type = "openai"

    if args.removeall:
        remove_blobs(None)
        remove_from_index(None)
    else:
        if not args.remove:
            create_search_index()

        print("Processing files...")
        if not args.datalakestorageaccount:
            print(f"Using local files in {args.files}")
            if args.localvectors:
                print(f'Using Local LLM for Embeddings\n')
            if args.localpdfparser:
                print(f'Using PyPdf local PDF parser (supports only digital PDFs) instead of Azure Form Recognizer service to extract text, tables and layout from the documents')
            read_files(args.files, use_vectors, compute_vectors_in_batch, args.openaideployment, args.openaimodelname)
        else:
            print(f"Using Data Lake Gen2 Storage Account {args.datalakestorageaccount}")
            read_adls_gen2_files(use_vectors, compute_vectors_in_batch, args.openaideployment, args.openaimodelname)