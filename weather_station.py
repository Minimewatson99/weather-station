import time
import math
import json
import threading
import paho.mqtt.client as mqtt
import smbus2
import bme280
import grovepi
import RPi.GPIO as GPIO

# --- Proprietary SwitchDoc Labs Imports ---
try:
    import SDL_Pi_HDC1000
    from SDL_Pi_Thunderboard_AS3935 import AS3935
    # import SDL_Pi_AfterShock
except ImportError as e:
    print(f"Missing SwitchDoc Labs library: {e}")
    exit(1)

# --- Configuration ---
MQTT_BROKER = "100.98.57.72"  # Replace with your Home Assistant IP
MQTT_PORT = 1883
MQTT_TOPIC = "home/weatherstation/state"
STATION_ALTITUDE_METERS = 117 # Replace with your actual elevation in meters

# --- Pin Definitions ---
LIGHT_SENSOR_PIN = 0 # GrovePi Analog Port A0
THUNDER_INT_PIN = 17 # RPi GPIO 17 (Pin 11)
# QUAKE_INT1_PIN = 27  # RPi GPIO 27 (Pin 13)
# QUAKE_INT2_PIN = 22  # RPi GPIO 22 (Pin 15)

# --- I2C Addresses ---
BME280_ADDRESS = 0x77 # Or 0x77 depending on your specific board

# --- Global State for Interrupt Data ---
event_data = {
    "lightning_detected": False,
    "lightning_distance_km": 0
#    "earthquake_detected": False,
#    "seismic_intensity": 0.0
}

# --- Initialization ---
print("Initializing Weather Station...")

# 1. Setup MQTT
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="PiWeatherStation")
try:
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
except Exception as e:
    print(f"Failed to connect to MQTT: {e}")

# 2. Setup I2C Bus & Sensors
bus = smbus2.SMBus(1)
calibration_params = bme280.load_calibration_params(bus, BME280_ADDRESS)

hdc1080 = SDL_Pi_HDC1000.SDL_Pi_HDC1000()
thunder = AS3935(address=0x02, bus=1) # 0x02 is the default ThunderBoard I2C address
# aftershock = SDL_Pi_AfterShock.SDL_Pi_AfterShock()

# Set ThunderBoard indoor/outdoor mode
thunder.set_indoors(True) # Set to True if testing inside
thunder.set_noise_floor(2)
thunder.calibrate(tun_cap=0x09) # 9 matches your test script!
thunder.set_min_strikes(1)

# 3. Setup GPIO Interrupts
GPIO.setmode(GPIO.BCM)
GPIO.setup(THUNDER_INT_PIN, GPIO.IN)
# GPIO.setup(QUAKE_INT1_PIN, GPIO.IN)
# GPIO.setup(QUAKE_INT2_PIN, GPIO.IN)

# --- Interrupt Callbacks ---
def handle_lightning(channel):
    global event_data
    # Read the interrupt register from the ThunderBoard
    interrupt_src = thunder.get_interrupt()
    if interrupt_src == 0x08:
        distance = thunder.get_distance()
        print(f"*** LIGHTNING STRIKE DETECTED! Distance: {distance} km ***")
        event_data["lightning_detected"] = True
        event_data["lightning_distance_km"] = distance
    elif interrupt_src == 0x04:
        print("Disturber detected (interference).")

# def handle_earthquake(channel):
#     global event_data
#     # If INT1 goes high, an earthquake is starting/occurring
#     if GPIO.input(QUAKE_INT1_PIN):
#         print("*** EARTHQUAKE DETECTED! Processing... ***")
#         event_data["earthquake_detected"] = True
#         # Read the Seismic Intensity (SI) from the D7S sensor
#         si_value = aftershock.readSI() 
#         event_data["seismic_intensity"] = si_value
#         print(f"Seismic Intensity: {si_value}")

# Attach Interrupts
GPIO.add_event_detect(THUNDER_INT_PIN, GPIO.RISING, callback=handle_lightning, bouncetime=100)
# GPIO.add_event_detect(QUAKE_INT1_PIN, GPIO.RISING, callback=handle_earthquake, bouncetime=200)

# --- Math Helpers ---
def calculate_dew_point(temp_c, humidity):
    if temp_c is None or humidity is None or humidity == 0:
        return None
    alpha = math.log(humidity / 100.0) + (17.625 * temp_c) / (243.04 + temp_c)
    dew_point = (243.04 * alpha) / (17.625 - alpha)
    return round(dew_point, 2)

def calculate_sea_level_pressure(pressure_hpa, temp_c, altitude_m):
    if pressure_hpa is None or temp_c is None:
        return None
    slp = pressure_hpa * math.pow((1 - ((0.0065 * altitude_m) / (temp_c + 0.0065 * altitude_m + 273.15))), -5.257)
    return round(slp, 2)

# --- Main Polling Loop ---
print("System Active. Polling sensors and waiting for events...")

try:
    while True:
        # 1. Read I2C Temp (HDC1080)
        # We will just use it for temperature since we know that half works!
        temp_c = hdc1080.readTemperature() 

        # 2. Read I2C Pressure & Humidity (BME280)
        bme_data = bme280.sample(bus, BME280_ADDRESS, calibration_params)
        raw_pressure = bme_data.pressure
        
        humidity = bme_data.humidity
        # 3. Read Analog Light (GrovePi)
        try:
            light_level = grovepi.analogRead(LIGHT_SENSOR_PIN)
        except IOError:
            light_level = -1

        # 4. Perform Derived Calculations
        dew_point = calculate_dew_point(temp_c, humidity)
        slp = calculate_sea_level_pressure(raw_pressure, temp_c, STATION_ALTITUDE_METERS)

        # 5. Build JSON Payload
        payload = {
            "temperature_c": round(temp_c, 2),
            "humidity_percent": round(humidity, 2),
            "dew_point_c": dew_point,
            "pressure_absolute_hpa": round(raw_pressure, 2),
            "pressure_sealevel_hpa": slp,
            "light_level": light_level,
            "lightning_detected": event_data["lightning_detected"],
            "lightning_distance_km": event_data["lightning_distance_km"]
            # "earthquake_detected": event_data["earthquake_detected"],
            # "seismic_intensity": event_data["seismic_intensity"]
        }

        # 6. Publish Data
        client.publish(MQTT_TOPIC, json.dumps(payload))
        print(f"Data Published: {payload}")

        # 7. Reset Event Flags
        event_data["lightning_detected"] = False
        event_data["lightning_distance_km"] = 0
        # event_data["earthquake_detected"] = False
        
        time.sleep(30)

except KeyboardInterrupt:
    print("\nShutting down gracefully...")
    GPIO.cleanup()
    client.loop_stop()
    client.disconnect()
    exit(0)