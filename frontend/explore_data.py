import sys, json, os
import base64
import time
from datetime import datetime
try:
    from azure.storage.blob import BlobServiceClient
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential
    AZURE_SDK_AVAILABLE = True
except ImportError:
    AZURE_SDK_AVAILABLE = False

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from document_chat import DocumentChatComponent

# Try to initialize Azure credential if SDK is available
COSMOS_INIT_ERROR = None
if AZURE_SDK_AVAILABLE:
    try:
        credential = DefaultAzureCredential()
        
        # Initialize Cosmos client for direct document access
        cosmos_url = os.getenv('COSMOS_URL')
        cosmos_db_name = os.getenv('COSMOS_DB_NAME')
        cosmos_documents_container = os.getenv('COSMOS_DOCUMENTS_CONTAINER_NAME')
        
        if cosmos_url and cosmos_db_name and cosmos_documents_container:
            cosmos_client = CosmosClient(cosmos_url, credential=credential)
            cosmos_database = cosmos_client.get_database_client(cosmos_db_name)
            cosmos_container = cosmos_database.get_container_client(cosmos_documents_container)
            COSMOS_AVAILABLE = True
        else:
            cosmos_client = None
            cosmos_container = None
            COSMOS_AVAILABLE = False
    except Exception as e:
        credential = None
        cosmos_client = None
        cosmos_container = None
        COSMOS_AVAILABLE = False
        COSMOS_INIT_ERROR = str(e)
else:
    credential = None
    cosmos_client = None
    cosmos_container = None
    COSMOS_AVAILABLE = False
    COSMOS_INIT_ERROR = "Azure SDK not available"


def format_finished(finished, error):
    return '✅' if finished else '❌' if error else '➖'

def parse_timestamp(timestamp_value):
    """Parse timestamp safely handling different data types"""
    if timestamp_value is None:
        return datetime.now()
    
    # If it's already a datetime object, return it
    if isinstance(timestamp_value, datetime):
        return timestamp_value
    
    # If it's a string, try to parse it
    if isinstance(timestamp_value, str):
        try:
            return datetime.fromisoformat(timestamp_value.replace('Z', '+00:00'))
        except ValueError:
            try:
                # Try alternative parsing for different formats
                return datetime.strptime(timestamp_value, '%Y-%m-%dT%H:%M:%S.%f')
            except ValueError:
                return datetime.now()
    
    # For any other type (int, float, etc.), return current time
    return datetime.now()

def get_documents_from_cosmos():
    """Fetch documents directly from Cosmos DB"""
    if not COSMOS_AVAILABLE:
        return []
    
    try:
        # Query all documents from Cosmos DB
        query = "SELECT * FROM c ORDER BY c._ts DESC"
        items = list(cosmos_container.query_items(
            query=query,
            enable_cross_partition_query=True
        ))
        return items
    except Exception as e:
        st.error(f"Error fetching documents from Cosmos DB: {e}")
        return []

@st.cache_data(ttl=15)  # Cache data for 15 seconds for faster UI updates
def get_documents_cached():
    """Get documents directly from Cosmos DB"""
    try:
        if COSMOS_AVAILABLE:
            # Use direct Cosmos DB access
            documents = get_documents_from_cosmos()
            if documents:
                return pd.json_normalize(documents)
        
        # If Cosmos not available, return empty dataframe
        return pd.DataFrame()
    except Exception as e:
        st.error(f"Error fetching data from Cosmos DB: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=300)  # Cache blob data for 5 minutes
def fetch_blob_cached(blob_name):
    """Cached version of blob fetching"""
    return fetch_blob_from_blob(blob_name)

@st.cache_data(ttl=60)  # Cache document details for 1 minute  
def fetch_document_details_cached(item_id):
    """Cached version of document details fetching - prioritizes direct Cosmos DB"""
    return fetch_json_from_cosmosdb(item_id)

def refresh_data():
    """Refresh data directly from Cosmos DB"""
    try:
        # Use cached version for better performance
        df = get_documents_cached()
        if not df.empty:
            return df
        else:
            st.info("📄 No documents found in Cosmos DB")
            return pd.DataFrame()
    except Exception as e:
        st.error(f"Error fetching data from Cosmos DB: {e}")
        return pd.DataFrame()
        if (AZURE_SDK_AVAILABLE and credential and 
            hasattr(st.session_state, 'cosmos_documents_container_name') and
            hasattr(st.session_state, 'cosmos_url') and
            hasattr(st.session_state, 'cosmos_db_name')):
            try:
                st.info("🔄 Trying direct Azure Cosmos DB connection...")
                return fetch_data_from_cosmosdb(st.session_state.cosmos_documents_container_name)
            except Exception as e2:
                st.error(f"Fallback to direct CosmosDB also failed: {e2}")
        
        return pd.DataFrame()

# Clear cache function removed - no longer needed

def fetch_data_from_cosmosdb(container_name):
    """Direct CosmosDB access - fallback method"""
    if not AZURE_SDK_AVAILABLE or not credential:
        raise Exception("Azure SDK not available or not authenticated")
    
    cosmos_client = CosmosClient(st.session_state.cosmos_url, credential)
    database = cosmos_client.get_database_client(st.session_state.cosmos_db_name)
    container = database.get_container_client(container_name)

    query = "SELECT * FROM c"
    items = list(container.query_items(query, enable_cross_partition_query=True))
    return pd.json_normalize(items)

def delete_item(dataset_name, file_name, item_id=None):
    """Delete item from both Cosmos DB and Blob Storage
    
    Args:
        dataset_name: Name of the dataset
        file_name: Name of the file
        item_id: Legacy parameter (not used, kept for compatibility)
    """
    if not COSMOS_AVAILABLE:
        st.error("❌ Cosmos DB not available. Cannot delete document.")
        return False
        
    if not AZURE_SDK_AVAILABLE:
        st.error("❌ Azure SDK not available. Cannot delete blob.")
        return False
        
    success_cosmos = False
    success_blob = False
    
    # Step 1: Delete from Cosmos DB first
    try:
        # Construct the correct Cosmos DB document ID: dataset_name__filename
        cosmos_doc_id = f"{dataset_name}__{file_name}"
        # Use empty dict as partition key (container is configured this way)
        cosmos_container.delete_item(item=cosmos_doc_id, partition_key={})
        success_cosmos = True
        st.success(f"✅ Deleted document {file_name} from Cosmos DB")
        
    except Exception as e:
        st.error(f"❌ Error deleting document from Cosmos DB: {e}")
        return False
    
    # Step 2: Delete from Blob Storage
    try:
        blob_url = st.session_state.get('blob_url') or os.getenv('BLOB_ACCOUNT_URL')
        container_name = st.session_state.get('container_name') or os.getenv('CONTAINER_NAME', 'datasets')
        
        if not blob_url:
            st.warning("⚠️ Blob storage URL not configured. Document deleted from Cosmos DB only.")
            return success_cosmos
        
        # Initialize blob service client with managed identity
        blob_service_client = BlobServiceClient(account_url=blob_url, credential=credential)
        
        # Construct blob path: {dataset_name}/{file_name}
        blob_name = f"{dataset_name}/{file_name}"
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
        
        # Check if blob exists before attempting to delete
        if blob_client.exists():
            blob_client.delete_blob()
            success_blob = True
            st.success(f"✅ Deleted file {file_name} from Blob Storage")
        else:
            st.warning(f"⚠️ File {file_name} not found in Blob Storage")
            success_blob = True  # Consider missing file as success
        
    except Exception as e:
        st.error(f"❌ Error deleting file from Blob Storage: {e}")
        st.warning("⚠️ Document was deleted from Cosmos DB but file may still exist in Blob Storage")
        return success_cosmos
    
    if success_cosmos and success_blob:
        st.success(f"🎉 Successfully deleted {file_name} from {dataset_name}")
        return True
    else:
        return success_cosmos  # Return true if at least Cosmos DB deletion succeeded

def reprocess_item(dataset_name, file_name, item_id=None):
    """Reprocess item by copying the blob to trigger the processing pipeline"""
    if not AZURE_SDK_AVAILABLE:
        st.error("❌ Azure SDK not available. Cannot reprocess file.")
        return False
        
    try:
        blob_url = st.session_state.get('blob_url') or os.getenv('BLOB_ACCOUNT_URL')
        container_name = st.session_state.get('container_name') or os.getenv('CONTAINER_NAME', 'datasets')
        
        if not blob_url:
            st.error("❌ Blob storage URL not configured. Cannot reprocess file.")
            return False
        
        # Initialize blob service client with managed identity
        blob_service_client = BlobServiceClient(account_url=blob_url, credential=credential)
        
        # Construct blob path: {dataset_name}/{file_name}
        blob_name = f"{dataset_name}/{file_name}"
        source_blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
        
        # Check if source blob exists
        if not source_blob_client.exists():
            st.error(f"❌ File {file_name} not found in Blob Storage. Cannot reprocess.")
            return False
        
        # Download the blob content
        blob_data = source_blob_client.download_blob().readall()
        
        # Get blob properties to preserve metadata
        blob_properties = source_blob_client.get_blob_properties()
        
        # Create a temporary copy by uploading the same content
        # This will trigger the blob processing pipeline
        temp_blob_name = f"{dataset_name}/.reprocess_{file_name}_{int(time.time())}"
        temp_blob_client = blob_service_client.get_blob_client(container=container_name, blob=temp_blob_name)
        
        # Upload temporary file
        temp_blob_client.upload_blob(blob_data, overwrite=True)
        
        # Add a small delay to ensure the temporary file is fully written
        time.sleep(0.5)
        
        # Copy it back to original location (overwrites and triggers processing)
        # Adding a timestamp to last_modified metadata to ensure change detection
        metadata = blob_properties.metadata.copy() if blob_properties.metadata else {}
        metadata['reprocessed_at'] = str(int(time.time()))
        
        source_blob_client.upload_blob(
            blob_data, 
            overwrite=True,
            metadata=metadata
        )
        
        # Clean up temporary file
        try:
            temp_blob_client.delete_blob()
        except:
            pass  # Ignore cleanup errors
        
        st.success(f"🔄 Successfully triggered reprocessing for {file_name}")
        st.info("⏳ Processing will begin automatically. Refresh the page in a few moments to see updated results.")
        return True
        
    except Exception as e:
        st.error(f"❌ Error reprocessing file: {e}")
        return False

@st.cache_data(ttl=300)  # Cache blob data for 5 minutes
def fetch_blob_from_blob_cached(blob_name):
    """Cached version of blob fetching"""
    return fetch_blob_from_blob(blob_name)

def fetch_blob_from_blob(blob_name):
    """Fetch blob data using direct Azure access if available"""
    # Ensure blob_name is a string to avoid TypeError
    if not isinstance(blob_name, str):
        blob_name = str(blob_name) if blob_name is not None else ''
    
    if (AZURE_SDK_AVAILABLE and credential and 
        hasattr(st.session_state, 'blob_url')):
        
        try:
            blob_service_client = BlobServiceClient(account_url=st.session_state.blob_url, credential=credential)
            
            # For dataset files, use the 'datasets' container
            if blob_name.startswith('datasets/'):
                container_name = 'datasets'
                # Remove the 'datasets/' prefix since it's now the container name
                blob_path = blob_name[9:]  # Remove 'datasets/' prefix
            else:
                # Fallback to the configured container for other blobs
                container_name = getattr(st.session_state, 'container_name', 'datasets')
                blob_path = blob_name

            container_client = blob_service_client.get_container_client(container_name)
            blob_client = container_client.get_blob_client(blob_path)

            blob_data = blob_client.download_blob().readall()
            return blob_data
        except Exception as e:
            st.error(f"❌ Failed to fetch blob data from {container_name}/{blob_path}: {e}")
            return None
    else:
        st.warning("Direct blob access not available - Azure SDK not configured")
        return None

@st.cache_data(ttl=300)  # Cache document details for 5 minutes  
def fetch_json_from_cosmosdb_cached(item_id):
    """Cached version of document detail fetching"""
    return fetch_json_from_cosmosdb(item_id)

def fetch_json_from_cosmosdb(item_id):
    """Fetch document details from CosmosDB directly"""
    if not COSMOS_AVAILABLE:
        st.error("❌ Cosmos DB not available. Cannot fetch document details.")
        return None
        
    try:
        # Direct CosmosDB access using the initialized client
        query = f"SELECT * FROM c WHERE c.id = '{item_id}'"
        
        items = list(cosmos_container.query_items(
            query=query,
            enable_cross_partition_query=True
        ))
        
        if items:
            return items[0]  # Return the first (and should be only) item
        else:
            st.warning(f"❌ Document {item_id} not found in Cosmos DB")
            return None
                
    except Exception as e:
        st.error(f"❌ Error fetching document from Cosmos DB: {e}")
        return None

def save_feedback_to_cosmosdb(item_id, rating, comments):
    """Save feedback using direct CosmosDB access if available"""
    if (AZURE_SDK_AVAILABLE and credential and 
        hasattr(st.session_state, 'cosmos_documents_container_name') and
        hasattr(st.session_state, 'cosmos_url') and
        hasattr(st.session_state, 'cosmos_db_name')):
        
        try:
            cosmos_client = CosmosClient(st.session_state.cosmos_url, credential)
            database = cosmos_client.get_database_client(st.session_state.cosmos_db_name)
            container = database.get_container_client(st.session_state.cosmos_documents_container_name)

            item = container.read_item(item=item_id, partition_key={})
            if 'feedback' not in item:
                item['feedback'] = []
            item['feedback'].append({'timestamp': datetime.utcnow().isoformat(), 'rating': rating, 'comments': comments})
            container.upsert_item(item)
            return True
        except Exception as e:
            st.error(f"Failed to save feedback: {e}")
            return False
    else:
        st.warning("Feedback functionality requires direct CosmosDB access")
        return False

def get_existing_feedback(item_id):
    """Get existing feedback using direct CosmosDB access if available"""
    if (AZURE_SDK_AVAILABLE and credential and 
        hasattr(st.session_state, 'cosmos_documents_container_name') and
        hasattr(st.session_state, 'cosmos_url') and
        hasattr(st.session_state, 'cosmos_db_name')):
        
        try:
            cosmos_client = CosmosClient(st.session_state.cosmos_url, credential)
            database = cosmos_client.get_database_client(st.session_state.cosmos_db_name)
            container = database.get_container_client(st.session_state.cosmos_documents_container_name)

            item = container.read_item(item=item_id, partition_key={})
            if 'feedback' in item and item['feedback']:
                return item['feedback'][-1]  # Return the most recent feedback
            return None
        except Exception as e:
            st.error(f"Failed to get feedback: {e}")
            return None
    else:
        return None

def explore_data_tab():
    """Main explore data tab with full functionality"""
    
    # Fetch data
    df = refresh_data()
    
    if df.empty:
        st.error('Failed to fetch data or no data found. If you submitted files for processing, please wait a few minutes and refresh the page. If problem remains, check your azure functionapp for errors and restart it.')
        return

    # Process documents into display format
    extracted_data = []
    for item in df.to_dict(orient='records'):
        # Handle different data formats - direct CosmosDB access
        if 'properties.blob_name' in item:
            # Direct CosmosDB format
            blob_name = item.get('properties.blob_name', '')
            errors = item.get('errors', '')
            
            # Ensure blob_name is a string to avoid TypeError
            if not isinstance(blob_name, str):
                blob_name = str(blob_name) if blob_name is not None else ''
            
            # Extract dataset and filename
            if '/' in blob_name and len(blob_name.split('/')) >= 2:
                parts = blob_name.split('/')
                dataset = parts[0] if parts[0] else parts[1]
                filename = '/'.join(parts[2:]) if len(parts) > 2 else parts[-1]
            else:
                dataset = 'unknown'
                filename = blob_name
            
            extracted_item = {
                'Dataset': dataset,
                'File Name': filename,
                'File Landed': format_finished(item.get('state.file_landed', False), errors),
                'OCR Extraction': format_finished(item.get('state.ocr_completed', False), errors),
                'GPT Extraction': format_finished(item.get('state.gpt_extraction_completed', False), errors),
                'GPT Evaluation': format_finished(item.get('state.gpt_evaluation_completed', False), errors),
                'GPT Summary': format_finished(item.get('state.gpt_summary_completed', False), errors),
                'Finished': format_finished(item.get('state.processing_completed', False), errors),
                'Request Timestamp': parse_timestamp(item.get('properties.request_timestamp', datetime.now().isoformat())),
                'Errors': errors,
                'Total Time': item.get('properties.total_time_seconds', 0),
                'Pages': item.get('properties.num_pages', 0),
                'Size': item.get('properties.blob_size', 0),
                'id': item['id'],
            }
        else:
            # CosmosDB direct format
            # Use the dataset field directly from the API response if available
            dataset = item.get('dataset') or 'unknown'
            file_name = item.get('file_name') or 'unknown'
            
            # If dataset is empty, try to parse from id
            if dataset == 'unknown' or dataset == '' or dataset is None:
                # Parse from id if available
                item_id = item.get('id', '')
                if '__' in item_id:
                    parts = item_id.split('__', 1)
                    dataset = parts[0] if parts[0] else 'unknown'
                    file_name = parts[1] if len(parts) > 1 and parts[1] else (file_name or 'unknown')
                elif '/' in item_id:
                    parts = item_id.split('/')
                    dataset = parts[0] if len(parts) > 1 and parts[0] else 'unknown'
                    file_name = '/'.join(parts[1:]) if len(parts) > 1 else (file_name or 'unknown')
            
            # Fallback: parse from blob_name or id if dataset field is not available
            if dataset == 'unknown':
                blob_name = item.get('blob_name', '') or item.get('properties', {}).get('blob_name', '') or item.get('id', '')
                
                # Ensure blob_name is a string to avoid TypeError
                if not isinstance(blob_name, str):
                    blob_name = str(blob_name) if blob_name is not None else ''
                
                # Parse dataset and filename from blob_name or id
                if '/' in blob_name:
                    parts = blob_name.split('/')
                    dataset = parts[0] if len(parts) > 1 else 'unknown'
                    file_name = '/'.join(parts[1:]) if len(parts) > 1 else blob_name
                elif '__' in blob_name:  # Handle dataset__filename format
                    parts = blob_name.split('__', 1)
                    dataset = parts[0] if len(parts) > 1 else 'unknown'
                    file_name = parts[1] if len(parts) > 1 else blob_name
                else:
                    dataset = 'unknown'
                    file_name = blob_name
            
            # Handle errors
            errors = item.get('errors', '') or item.get('error', '')
            
            # Extract state information (pd.json_normalize flattens nested objects)
            # So state.file_landed becomes 'state.file_landed' key
            extracted_item = {
                'Dataset': dataset,
                'File Name': file_name,
                'File Landed': format_finished(item.get('state.file_landed', False), errors),
                'OCR Extraction': format_finished(item.get('state.ocr_completed', False), errors),
                'GPT Extraction': format_finished(item.get('state.gpt_extraction_completed', False), errors),
                'GPT Evaluation': format_finished(item.get('state.gpt_evaluation_completed', False), errors),
                'GPT Summary': format_finished(item.get('state.gpt_summary_completed', False), errors),
                'Finished': format_finished(item.get('state.processing_completed', False), errors),
                'Request Timestamp': parse_timestamp(item.get('created_at', datetime.now().isoformat())),
                'Errors': errors,
                'Total Time': item.get('total_time', 0),
                'Pages': item.get('pages', 0),
                'Size': item.get('size', 0),
                'id': item['id'],
            }
        
        extracted_data.append(extracted_item)

    extracted_df = pd.DataFrame(extracted_data)
    extracted_df.insert(0, 'Select', False)
    extracted_df = extracted_df.sort_values(by='Request Timestamp', ascending=False)

    # Filters
    filter_col1, filter_col2, filter_col3 = st.columns([3, 1, 1])

    with filter_col1:
        filter_dataset = st.multiselect("Dataset", options=extracted_df['Dataset'].unique(), default=extracted_df['Dataset'].unique())

    with filter_col2:
        filter_finished = st.selectbox("Processing Status", options=['All', 'Finished', 'Not Finished'], index=0)

    with filter_col3:
        filter_date_range = st.date_input("Request Date Range", [])

    # Apply filters
    filtered_df = extracted_df[
        extracted_df['Dataset'].isin(filter_dataset) &
        (extracted_df['Finished'].apply(lambda x: True if filter_finished == 'All' else (x == '✅' if filter_finished == 'Finished' else (x == '❌' or x == '➖')))) &
        (extracted_df['Request Timestamp'].apply(lambda x: (not filter_date_range) or (len(filter_date_range) == 2 and x.date() >= filter_date_range[0] and x.date() <= filter_date_range[1])))
    ]

    # Main content
    cols = st.columns([0.5, 10, 0.5])
    with cols[1]:
        tabs_ = st.tabs(["🧮 Table", "📐 Analytics"])

        with tabs_[0]:
            # Data table with selection
            edited_df = st.data_editor(filtered_df, column_config={"id": None})
            selected_rows = edited_df[edited_df['Select'] == True]

            # Action buttons
            sub_col = st.columns([1, 1, 1, 3])

            with sub_col[0]:
                if st.button('Refresh Table', key='refresh_table'):
                    # Clear all cached data before rerunning
                    get_documents_cached.clear()
                    fetch_json_from_cosmosdb_cached.clear()
                    fetch_blob_from_blob_cached.clear()
                    st.rerun()

            with sub_col[1]:
                if st.button('Delete Selected', key='delete_selected'):
                    for _, row in selected_rows.iterrows():
                        delete_item(row['Dataset'], row['File Name'], row['id'])
                    # Clear cache and refresh to reflect deletions
                    get_documents_cached.clear()
                    fetch_json_from_cosmosdb_cached.clear()
                    st.rerun()

            with sub_col[2]:
                if st.button('Re-process Selected', key='reprocess_selected'):
                    for _, row in selected_rows.iterrows():
                        reprocess_item(row['Dataset'], row['File Name'], row['id'])
                    # Clear cache and refresh to show updated status
                    get_documents_cached.clear()
                    fetch_json_from_cosmosdb_cached.clear()
                    st.rerun()

            # Document details for single selection
            if len(selected_rows) == 1:
                st.markdown("---")
                st.markdown(f"###### {selected_rows.iloc[0]['File Name']}")

                selected_item = selected_rows.iloc[0]
                # Construct the correct blob path: datasets/{dataset_name}/{filename}
                blob_name = f"datasets/{selected_item['Dataset']}/{selected_item['File Name']}"
                json_item_id = selected_item['id']
                
                # Human-in-the-loop feedback (if direct Azure access available)
                if AZURE_SDK_AVAILABLE and credential:
                    with st.expander("Human in the loop Feedback"):
                        feedback = get_existing_feedback(json_item_id)
                        initial_rating = feedback['rating'] if feedback else None
                        initial_comments = feedback['comments'] if feedback else ""

                        # Feedback section with rating and comments
                        feedback_col1, feedback_col2 = st.columns([1, 2])
                        with feedback_col1:
                            rating = st.slider("Extraction Quality", 1, 10, initial_rating, key="rating")
                        with feedback_col2:
                            comments = st.text_area("Comments on the Extraction", initial_comments, key="comments")

                        if st.button("Submit Feedback"):
                            if save_feedback_to_cosmosdb(json_item_id, rating, comments):
                                st.success("Feedback submitted!")

                # File preview and JSON data with caching
                blob_data = None
                if AZURE_SDK_AVAILABLE and credential:
                    with st.spinner('Loading file...'):
                        blob_data = fetch_blob_from_blob_cached(blob_name)

                # Fetch JSON data with caching
                with st.spinner('Loading document details...'):
                    json_data = fetch_json_from_cosmosdb_cached(json_item_id)

                # Display content in two columns
                pdf_col, json_col = st.columns(2)
                
                # File preview column
                with pdf_col:
                    if blob_data:
                        file_extension = selected_item['File Name'].split('.')[-1].lower()
                        
                        if file_extension == 'pdf':
                            # Robust PDF display with reliable fallback
                            file_size_mb = len(blob_data) / (1024 * 1024)
                            
                            # Ensure blob_name is a string to avoid TypeError
                            if not isinstance(blob_name, str):
                                blob_name = str(blob_name) if blob_name is not None else ''
                            filename = blob_name.split("/")[-1]
                            
                            try:
                                pdf_base64 = base64.b64encode(blob_data).decode('utf-8')
                                
                                if file_size_mb > 15:  # Very large files - download only
                                    st.warning(f'PDF file is very large ({file_size_mb:.1f}MB). Please use the download button below to view the file.')
                                else:
                                    # Try to display PDF with robust fallback
                                    st.info(f"📄 PDF Preview ({file_size_mb:.1f}MB)")
                                    
                                    try:
                                        # Embedded PDF viewer using iframe (most compatible)
                                        pdf_display = f'''
                                        <div style="position: relative; width: 100%; height: 800px; border: 1px solid #ddd; border-radius: 8px; overflow: hidden;">
                                            <iframe 
                                                src="data:application/pdf;base64,{pdf_base64}" 
                                                width="100%" 
                                                height="100%" 
                                                frameborder="0"
                                                style="border: none;">
                                                <div style="padding: 20px; text-align: center;">
                                                    <p>Your browser cannot display PDFs inline.</p>
                                                    <a href="data:application/pdf;base64,{pdf_base64}" download="{filename}" 
                                                       style="background-color: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; border-radius: 4px;">
                                                       Download PDF
                                                    </a>
                                                </div>
                                            </iframe>
                                        </div>
                                        '''
                                        st.markdown(pdf_display, unsafe_allow_html=True)
                                        
                                        # Additional fallback message
                                        st.caption("💡 If the PDF doesn't display properly, use the download button below.")
                                        
                                    except Exception as e:
                                        st.error(f"Error displaying PDF: {str(e)}")
                                        st.info("Please use the download button below to access the file.")
                                
                                # Download button below the preview
                                download_link = f'<a href="data:application/octet-stream;base64,{pdf_base64}" download="{filename}" style="text-decoration: none;"><button style="background-color: #4CAF50; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; margin-top: 10px;">📥 Download {filename}</button></a>'
                                st.markdown(download_link, unsafe_allow_html=True)
                                        
                            except Exception as e:
                                st.error(f"Error processing PDF file: {str(e)}. File may be corrupted or too large.")
                                st.info("Try refreshing the page or contact support if the issue persists.")
                                
                        elif file_extension in ['jpeg', 'jpg', 'png', 'bmp', 'tiff', 'heif']:
                            # Image display
                            image_base64 = base64.b64encode(blob_data).decode('utf-8')
                            image_display = f'<img src="data:image/{file_extension};base64,{image_base64}" width="100%"/>'
                            st.markdown(image_display, unsafe_allow_html=True)
                            
                        elif file_extension in ['docx', 'xlsx', 'pptx', 'html']:
                            # Download link for other Office formats
                            # Ensure blob_name is a string to avoid TypeError
                            if not isinstance(blob_name, str):
                                blob_name = str(blob_name) if blob_name is not None else ''
                            download_link = f'<a href="data:application/octet-stream;base64,{base64.b64encode(blob_data).decode("utf-8")}" download="{blob_name.split("/")[-1]}">Download {file_extension.upper()}</a>'
                            st.markdown(download_link, unsafe_allow_html=True)
                        else:
                            st.warning(f'Unsupported file type: {file_extension}')
                    else:
                        st.info("File preview not available - Azure SDK access required")
                
                # Document data column
                with json_col:
                    if json_data:
                        tabs = st.tabs(["GPT Extraction", "OCR Extraction", "GPT Evaluation", "GPT Summary", "Processing Details", "Chat with Document"])
                        
                        # GPT Extraction Tab
                        with tabs[0]:
                            try:
                                gpt_extraction = json_data.get('extracted_data', {}).get('gpt_extraction_output')
                                if gpt_extraction:
                                    # Download button for GPT extraction
                                    st.download_button(
                                        label="Download GPT Extraction",
                                        data=json.dumps(gpt_extraction, indent=2) if isinstance(gpt_extraction, dict) else str(gpt_extraction),
                                        file_name="gpt_extraction.json",
                                        mime="application/json"
                                    )
                                    if isinstance(gpt_extraction, dict):
                                        st.json(gpt_extraction)
                                    else:
                                        st.text(gpt_extraction)
                                else:
                                    st.warning("GPT extraction data not available")
                            except Exception as e:
                                st.warning(f"Error displaying GPT extraction: {str(e)}")
                        
                        # OCR Extraction Tab
                        with tabs[1]:
                            try:
                                ocr_data = json_data.get('extracted_data', {}).get('ocr_output')
                                if ocr_data:
                                    # Download button for OCR data
                                    st.download_button(
                                        label="Download OCR Data",
                                        data=ocr_data,
                                        file_name="ocr_extraction.txt",
                                        mime="text/plain"
                                    )
                                    st.text(ocr_data)
                                else:
                                    st.warning("OCR extraction data not available")
                            except Exception as e:
                                st.warning(f"Error displaying OCR data: {str(e)}")
                        
                        # GPT Evaluation Tab
                        with tabs[2]:
                            try:
                                evaluation_data = json_data.get('extracted_data', {}).get('gpt_extraction_output_with_evaluation')
                                if evaluation_data:
                                    st.info("Evaluation works best with a Reasoning Model such as OpenAI O1.") 
                                    # Download button for evaluation data
                                    st.download_button(
                                        label="Download Evaluation Data",
                                        data=json.dumps(evaluation_data, indent=2) if isinstance(evaluation_data, dict) else str(evaluation_data),
                                        file_name="gpt_evaluation.json",
                                        mime="application/json"
                                    )
                                    if isinstance(evaluation_data, dict):
                                        st.json(evaluation_data)
                                    else:
                                        st.text(evaluation_data)
                                else:
                                    st.warning("GPT evaluation data not available")
                            except Exception as e:
                                st.warning(f"Error displaying evaluation data: {str(e)}")
                        
                        # Summary Tab
                        with tabs[3]:
                            try:
                                summary_data = json_data.get('extracted_data', {}).get('gpt_summary_output')
                                if summary_data:
                                    # Download button for summary
                                    st.download_button(
                                        label="Download Summary",
                                        data=summary_data,
                                        file_name="gpt_summary.md",
                                        mime="text/markdown"
                                    )
                                    st.markdown(summary_data)
                                else:
                                    st.warning("Summary data not available")
                            except Exception as e:
                                st.warning(f"Error displaying summary: {str(e)}")
                        
                        # Processing Details Tab
                        with tabs[4]:
                            try:
                                properties = json_data.get('properties', {})
                                state = json_data.get('state', {})
                                model_input = json_data.get('model_input', {})
                                
                                # Create a more readable format for the details
                                details_data = [
                                    ["File ID", str(json_data.get('id', 'N/A'))],
                                    ["Blob Name", str(properties.get('blob_name', 'N/A'))],
                                    ["Blob Size", f"{properties.get('blob_size', 0)} bytes"],
                                    ["Number of Pages", str(properties.get('num_pages', 'N/A'))],
                                    ["Total Processing Time", f"{properties.get('total_time_seconds', 0):.2f} seconds"],
                                    ["Request Timestamp", str(properties.get('request_timestamp', 'N/A'))],
                                    ["File Landing Time", f"{state.get('file_landed_time_seconds', 0):.2f} seconds"],
                                    ["OCR Processing Time", f"{state.get('ocr_completed_time_seconds', 0):.2f} seconds"],
                                    ["GPT Extraction Time", f"{state.get('gpt_extraction_completed_time_seconds', 0):.2f} seconds"],
                                    ["GPT Evaluation Time", f"{state.get('gpt_evaluation_completed_time_seconds', 0):.2f} seconds"],
                                    ["GPT Summary Time", f"{state.get('gpt_summary_completed_time_seconds', 0):.2f} seconds"],
                                    ["Model Deployment", str(model_input.get('model_deployment', 'N/A'))],
                                    ["Model Prompt", str(model_input.get('model_prompt', 'N/A'))]
                                ]
                                
                                # Convert to DataFrame for better display - ensure all values are strings
                                df_details = pd.DataFrame(details_data, columns=['Metric', 'Value'])
                                df_details['Value'] = df_details['Value'].astype(str)
                                
                                # Display table
                                st.table(df_details)
                                
                            except Exception as e:
                                st.warning(f"Some details are not available: {str(e)}")
                        
                        # Chat with Document Tab
                        with tabs[5]:
                            try:
                                # Import the chat component
                                from document_chat import render_document_chat_tab
                                
                                # Get backend URL from session state
                                backend_url = st.session_state.get('backend_url', 'http://localhost:8000')
                                
                                # Get document context from extracted data
                                extracted_data = json_data.get('extracted_data', {})
                                gpt_extraction = extracted_data.get('gpt_extraction_output', {})
                                
                                # Convert to JSON string for API
                                document_context = json.dumps(gpt_extraction) if gpt_extraction else "{}"
                                
                                # Render the chat interface
                                render_document_chat_tab(
                                    document_id=json_item_id,
                                    document_name=selected_item['File Name'],
                                    backend_url=backend_url,
                                    document_context=document_context
                                )
                                
                            except Exception as e:
                                st.error(f"Error loading chat interface: {e}")
                                st.info("Please make sure the backend is running and accessible.")
                        
                    else:
                        st.error("No document details available")

            elif len(selected_rows) > 1:
                st.warning('Please select exactly one item to show extraction.')

        # Analytics tab
        with tabs_[1]:
            col1, col2 = st.columns(2)

            with col1:
                try:
                    success_counts = filtered_df['Finished'].value_counts()
                    labels = ['Successful', 'Processing', 'Not Successful']
                    sizes = [success_counts.get('✅', 0), success_counts.get('➖', 0), success_counts.get('❌', 0)]
                    colors = ['green', 'orange', 'red']

                    fig3 = go.Figure(data=[go.Pie(labels=labels, values=sizes, marker=dict(colors=colors))])
                    fig3.update_traces(textinfo='label+percent', textfont_size=12)
                    fig3.update_layout(title_text='Processing Status')
                    st.plotly_chart(fig3)
                except Exception as e:
                    st.error(f"Error in creating the pie chart: {e}")

            with col2:
                try:
                    fig1 = px.histogram(filtered_df, x='Dataset', title='Number of Files per Dataset', labels={'x': 'Dataset', 'y': 'Number of Files'})
                    fig1.update_layout(xaxis_title_text='Dataset', yaxis_title_text='Number of Files')
                    st.plotly_chart(fig1)
                except Exception as e:
                    st.error(f"Error in creating the histogram: {e}")

            col3, col4 = st.columns([1, 1])

            with col3:
                try:
                    fig2 = px.histogram(filtered_df, x='Total Time', nbins=20, title='Distribution of Processing Time', labels={'x': 'Processing Time (seconds)', 'y': 'Number of Files'})
                    fig2.update_layout(xaxis_title_text='Processing Time (seconds)', yaxis_title_text='Number of Files')
                    st.plotly_chart(fig2)
                except Exception as e:
                    st.error(f"Error in creating the histogram: {e}")

            with col4:
                try:
                    fig5 = px.scatter(filtered_df, x='Size', y='Total Time', title='Processing Time vs. File Size', labels={'x': 'File Size (bytes)', 'y': 'Processing Time (seconds)'})
                    fig5.update_layout(xaxis_title_text='File Size (bytes)', yaxis_title_text='Processing Time (seconds)')
                    st.plotly_chart(fig5)
                except Exception as e:
                    st.error(f"Error in creating the scatter plot: {e}")

            col5, col6 = st.columns([1, 1])
            with col5:
                try:
                    fig4 = px.scatter(filtered_df[filtered_df['Pages'] > 0], x='Request Timestamp', y='Total Time', color='Pages', title='Processing Time per Page by Request Timestamp', labels={'x': 'Request Timestamp', 'y': 'Processing Time (seconds)'})
                    fig4.update_layout(xaxis_title_text='Request Timestamp', yaxis_title_text='Processing Time (seconds)')
                    st.plotly_chart(fig4)
                except Exception as e:
                    st.error(f"Error in creating the scatter plot: {e}")
            with col6:
                try:
                    fig6 = px.histogram(filtered_df, x='Pages', title='Number of Pages per File', labels={'x': 'Number of Pages', 'y': 'Number of Files'})
                    fig6.update_layout(xaxis_title_text='Number of Pages', yaxis_title_text='Number of Files')
                    st.plotly_chart(fig6)
                except Exception as e:
                    st.error(f"Error in creating the histogram: {e}")
