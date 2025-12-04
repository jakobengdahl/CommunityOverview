
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
