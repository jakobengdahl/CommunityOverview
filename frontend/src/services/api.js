
const API_URL = '/chat'; // Using proxy in Vite or direct URL

/**
 * Send a message to the backend chat endpoint
 * @param {Array} messages - Conversation history
 * @returns {Promise<Object>} Backend response
 */
export async function sendMessageToBackend(messages) {
  try {
    const response = await fetch(API_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ messages }),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('Error communicating with backend:', error);
    throw error;
  }
}

/**
 * Execute a backend tool directly
 * @param {string} toolName - Name of the tool to execute
 * @param {Object} arguments - Arguments for the tool
 * @returns {Promise<Object>} Tool result
 */
export async function executeTool(toolName, args) {
  try {
    const response = await fetch('/execute_tool', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tool_name: toolName,
        arguments: args
      }),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error(`Error executing tool ${toolName}:`, error);
    throw error;
  }
}

/**
 * Upload a file to the backend to extract text
 * @param {File} file - The file to upload
 * @returns {Promise<Object>} Response with extracted text
 */
export async function uploadFileToBackend(file) {
  try {
    const formData = new FormData();
    formData.append('file', file);

    const response = await fetch('/upload', {
      method: 'POST',
      body: formData, // Content-Type is set automatically for FormData
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('Error uploading file:', error);
    throw error;
  }
}

/**
 * Load a saved visualization view by name
 * @param {string} viewName - Name of the view to load
 * @returns {Promise<Object>} View data with nodes and metadata
 */
export async function loadVisualizationView(viewName) {
  try {
    const result = await executeTool('get_visualization', { name: viewName });

    if (!result.success) {
      throw new Error(result.error || `View "${viewName}" not found`);
    }

    return result.view;
  } catch (error) {
    console.error(`Error loading view ${viewName}:`, error);
    throw error;
  }
}
