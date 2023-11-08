from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from dotenv import load_dotenv
import os
from datetime import datetime
import pytz

# Load environment variables from .env file
load_dotenv()

# Get the service endpoint and API key from the environment
endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")
key = os.getenv("AZURE_SEARCH_API_KEY")
index = os.getenv("AZURE_SEARCH_INDEX")

# Get current time in US Eastern Time
now = datetime.now(pytz.timezone('US/Eastern'))

# Convert to ISO 8601 format
timestamp = now.isoformat()

# Create a client
credential = AzureKeyCredential(key)
client = SearchClient(endpoint=endpoint,
                      index_name=index,
                      credential=credential)

# Define the new fields
new_fields = {
    "category": "Data Management, Practice Maturity, Measurement, Organizational Assessment, Data Flow Coordination",
    "timestamp": timestamp,
    "title": "Measuring Data Management Practice Maturity",
    "url": "https://mattang.sharepoint.com/:b:/s/MattangKnowledgeCenter/EVnb7mwXeq1ItJFF_dqe_VQBsDVwvcr9m3M2-yKQZMaBHg?e=ByC0Fp"
}

# Get all documents with the specific sourcefile
results = client.search(search_text="Measuring Data Management Practice Maturity.pdf", include_total_count=True)

# Loop through the results and update each document
for result in results:
    # Create a new document with the same id and updated fields
    document = {"id": result["id"], **new_fields}

    # Update the document in the index
    merge_result = client.merge_documents(documents=[document])

    # Check if the operation was successful
    if not merge_result[0].succeeded:
        print(f"Failed to update document: {merge_result[0].key}")
    else:
        print(f"Successfully updated document: {merge_result[0].key}")