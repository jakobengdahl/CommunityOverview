from playwright.sync_api import sync_playwright, expect

def test_chat(page):
    # Go to app with a community selected to enable chat
    page.goto("http://localhost:3000/?community=TestCommunity")

    # Wait for chat input
    expect(page.locator(".chat-input")).to_be_visible(timeout=10000)

    # Type a message
    page.fill(".chat-input", "Hej, finns det några initiativ om AI?")

    # Click send
    page.click(".chat-send-button")

    # Wait for response (which might fail if backend not reachable, but we will see screenshot)
    # The message should appear in chat
    expect(page.locator(".chat-message.user")).to_contain_text("Hej, finns det några initiativ om AI?")

    # Wait a bit for backend response (even if it errors, we want to see it)
    page.wait_for_timeout(3000)

    # Take screenshot
    page.screenshot(path="verification/verification.png")

if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            test_chat(page)
        except Exception as e:
            print(f"Test failed: {e}")
            page.screenshot(path="verification/verification_failed.png")
        finally:
            browser.close()
