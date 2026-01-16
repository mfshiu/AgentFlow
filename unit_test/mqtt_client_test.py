import paho.mqtt.client as mqtt

# ---- 連線參數 ----
broker_type = "mqtt"
host = "localhost"
port = 1884
username = "eric"
password = "eric123"
keepalive = 60

# ---- callback ----
def on_connect(client, userdata, flags, reasonCode, properties=None):
    print(f"✅ Connected to {host}:{port} with result code {reasonCode}")
    # 成功連線後自動訂閱一個測試 topic
    client.subscribe("test/topic")

def on_message(client, userdata, msg):
    print(f"📩 Received message: {msg.topic} -> {msg.payload.decode()}")

# ---- 建立 client ----
client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

if username:  # 有設定帳號才呼叫
    client.username_pw_set(username, password)

client.on_connect = on_connect
client.on_message = on_message

print("🚀 Connecting...")
client.connect(host, port, keepalive)

# 啟動非同步 loop
client.loop_start()

# 測試發送訊息
client.publish("test/topic", payload="Hello MQTT!")

# 保持程式運作一段時間觀察
import time
time.sleep(50)

client.loop_stop()
client.disconnect()
print("🛑 Disconnected")
