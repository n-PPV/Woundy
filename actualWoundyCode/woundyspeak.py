import requests
import matplotlib.pyplot as plt

CHANNEL_ID = "3256717"
READ_API_KEY = "GI01L24DNG172U9B"

BASE_URL = f"https://api.thingspeak.com/channels/{CHANNEL_ID}/feeds.json?api_key={READ_API_KEY}"

# Step 1: Fetch the tail to find sentinels and measurement count
# We expect: ..., meas_count, -42, -42, -42 at the end
tail = requests.get(f"{BASE_URL}&results=10").json()
tail_values = []
for feed in tail['feeds']:
    value = feed.get('field1')
    if value is not None:
        tail_values.append(int(value))

# Find the last three -42s and read the value just before them
# Walk backwards: expect -42, -42, -42, then meas_count
if (len(tail_values) >= 4
    and tail_values[-1] == -42
    and tail_values[-2] == -42
    and tail_values[-3] == -42):
    meas_count = tail_values[-4]
    print(f"[Woundy] Reported measurement count: {meas_count}")
else:
    print("[Woundy] Could not find end sentinels. Using all available data.")
    meas_count = None

# Step 2: Fetch the full scan data
# Total entries sent = 3 (-17s) + actual_measurements + 1 (count) + 3 (-42s)
if meas_count is not None:
    total_entries = 3 + meas_count + 1 + 3
    resp = requests.get(f"{BASE_URL}&results={total_entries}").json()
else:
    resp = requests.get(f"{BASE_URL}&results=500").json()

raw = []
for feed in resp['feeds']:
    value = feed.get('field1')
    if value is not None:
        raw.append(int(value))

# Step 3: Strip sentinels — keep only the actual distance measurements
# Skip leading -17s, trailing count + -42s
start = 0
while start < len(raw) and raw[start] == -17:
    start += 1

end = len(raw)
# Remove trailing -42s
while end > start and raw[end - 1] == -42:
    end -= 1
# Remove the measurement count value
if end > start:
    end -= 1

distances = raw[start:end]
print(f"[Woundy] Plotting {len(distances)} measurements")

plt.plot(distances, marker='o', color='blue')
plt.title("Woundy: Απόσταση κατά την κίνηση")
plt.xlabel("Αριθμός Μέτρησης")
plt.ylabel("Απόσταση (mm)")
plt.grid(True)
plt.show()