# Quick Troubleshooting Guide - "Failed to export graph"

You reported: Export test passes, but browser shows "Failed to export graph: Failed to fetch" with **no console logs**.

## 🚨 Most Likely Causes

1. **Browser cache** - Old JavaScript is still loaded
2. **Backend not running** - Server crashed or didn't start

## ✅ Solution (Do This First!)

### Option A: Use Logging-Enabled Startup Script

Stop `./start-dev.sh` if running (Ctrl+C), then:

```bash
./start-with-logs.sh
```

This shows **all backend and frontend logs** directly in the terminal with `[BACKEND]` and `[FRONTEND]` prefixes.

You will see:
- `[BACKEND] [Export] Starting graph export...` when you click Export
- `[BACKEND] [Export] ERROR:` if something fails
- All startup errors clearly labeled

### Option B: Run Troubleshooting Script

While `./start-dev.sh` is running, open a **second terminal** and run:

```bash
./troubleshoot.sh
```

This will:
- Check if backend is responding ✓
- Test export endpoint ✓
- Check if frontend is running ✓
- Verify logging code exists ✓
- Give you exact instructions for browser testing

## 📋 Step-by-Step (If Still Not Working)

### 1. Start with Logs

```bash
# Stop any running instances
pkill -f "python.*server.py"
pkill -f "vite|npm"

# Start with logging enabled
./start-with-logs.sh
```

### 2. Open Browser Correctly

**IMPORTANT:** Use the correct port configured in vite.config.js:

✅ Correct: `http://localhost:3000`

### 3. Hard Refresh Browser

Your browser has cached the OLD JavaScript without logging. You **MUST** do a hard refresh:

**Windows/Linux:**
```
Ctrl + Shift + R
```

**Mac:**
```
Cmd + Shift + R
```

### 4. Open DevTools

Press **F12** or right-click → Inspect → Console tab

### 5. Click Export Graph

Click the Export button in the header.

### 6. Check BOTH Terminals and Browser

**Terminal (where start-with-logs.sh runs):**
```
[BACKEND] [Export] Starting graph export...
[BACKEND] [Export] Total nodes in storage: X
[BACKEND] [Export] Successfully dumped X nodes
```

**Browser Console:**
```
[Header] Starting graph export...
[Header] Fetching from http://localhost:8000/export_graph
[Header] Response status: 200
[Header] Export data received: { nodes: X, edges: Y }
```

## 🔍 Diagnostic Decision Tree

### Scenario 1: No logs in Browser Console

**Cause:** JavaScript not refreshed or wrong URL

**Solution:**
1. Verify URL is `http://localhost:3000`
2. Hard refresh: Ctrl+Shift+R
3. Clear browser cache completely
4. Check DevTools → Sources tab → See if `Header.jsx` contains `console.log('[Header]')`

### Scenario 2: Logs in Browser but "Failed to fetch"

**Cause:** Backend not running or not accessible

**Solution:**
1. Check terminal for `[BACKEND]` logs
2. Look for startup errors or crashes
3. Run: `curl http://localhost:8000/export_graph`
4. If curl fails, backend isn't running

### Scenario 3: Backend logs show [Export] ERROR

**Cause:** Export endpoint crashed

**Solution:**
1. Read the full traceback in terminal
2. Look for missing dependencies or import errors
3. Check if `graph.json` file is corrupted

### Scenario 4: Backend logs show success but download fails

**Cause:** Frontend issue or browser security

**Solution:**
1. Check browser Console for download errors
2. Check browser download permissions
3. Try a different browser

## 🧪 Verify Export Logic Works

Even if server won't start, verify the core logic:

```bash
cd mcp-server
python3 test_export_logic.py
```

If this passes (which it does based on your output), the **serialization logic is correct**. The problem is with:
- Server startup
- Network connectivity
- Browser caching
- Wrong URL

## 📞 Report Back

After trying the above, report:

1. **Which method did you use?**
   - [ ] start-with-logs.sh
   - [ ] troubleshoot.sh
   - [ ] start-dev.sh

2. **What URL are you opening?**
   - Answer: _______________

3. **Did you do a hard refresh (Ctrl+Shift+R)?**
   - [ ] Yes
   - [ ] No

4. **Backend logs show (from terminal):**
   ```
   Paste [BACKEND] logs here
   ```

5. **Browser console shows:**
   ```
   Paste browser console output here
   ```

6. **Error message (if any):**
   ```
   Paste exact error message
   ```

## 🎯 Most Common Fix

Based on "no logs in console", the issue is **99% browser cache**:

```bash
# 1. Hard refresh in browser (Ctrl+Shift+R)
# 2. If that doesn't work, clear all browser cache
# 3. If that doesn't work, try different browser
# 4. Verify you're on http://localhost:3000
```
