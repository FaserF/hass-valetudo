# Valetudo for Home Assistant (Unofficial Fork)

> [!IMPORTANT]
> This is an **unofficial fork** of the [official Home Assistant Valetudo integration](https://github.com/Hypfer/hass-valetudo).
> Since the original repository often does not accept new Pull Requests, this fork was created to implement and maintain additional features for the community. This fork will be updated from the source branch, except the README.

This `custom_component` provides enhanced functionality for using Valetudo-enabled vacuum robots within Home Assistant.

## Features

### 🆕 Firmware Update Entity
Native Home Assistant firmware update support!
- View currently installed Valetudo version.
- Automatically check for the latest Valetudo releases from GitHub.
- Trigger firmware downloads directly from Home Assistant via MQTT.

### 🆕 MQTT Auto-Discovery
Easier setup with smart discovery!
- Automatically discover Valetudo robots on your network via MQTT.
- Robust filtering ensures that only Valetudo devices are identified, avoiding false positives with other MQTT devices like OpenWrt.

### Icons
<img width="700" src="https://github.com/user-attachments/assets/00131949-896b-45f7-a994-5f8aa664713d" />

### 🆕 Room Selection
Select and clean specific rooms directly from Home Assistant.
- Dynamic "Select" entity populated from Valetudo map data.
- Built-in `valetudo.clean_room` service for easy automation.
- Supports multiple iterations per room.

### 🆕 Advanced Augmentations (Optional)
This integration provides extra sensors and controls for advanced users. **These are disabled by default** to keep your dashboard clean.
- **Consumables**: Native sensors for Main Brush and Filter endurance.
- **Extended Configuration**: Voice Volume slider, Carpet Boost switch, and **Locate Robot** button.
- **Network Diagnostics**: Separate sensors for Wi-Fi SSID and Signal Strength.
- **Improved Device Mapping**: Automatically normalizes and adds the robot's MAC address to the device registry for better network integration (e.g., with Fritz!Box or OpenWrt).

<img width="700" src="https://github.com/user-attachments/assets/a6379c49-4e53-43c0-b914-1f92bcb61f6e" />


## Installation

### Via HACS (Recommended)

To install this integration using [HACS](https://www.hacs.xyz/):

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.homeassistant.io/badges/hacs_repository.svg)](https://my.homeassistant.io/redirect/hacs_repository/?repository=https://github.com/FaserF/hass-valetudo&category=Integration)

1.  Open **HACS** in Home Assistant.
2.  Click on **Integrations**.
3.  Click the three dots in the top right corner and select **Custom repositories**.
4.  Add the following URL: `https://github.com/FaserF/hass-valetudo`
5.  Select **Integration** as the Category and click **Add**.
6.  You can now search for and install the "Valetudo" integration.

### Manual Installation

If you don't want to use HACS, you can install the integration manually:
1.  Download the latest release.
2.  Unpack the `custom_components/valetudo` folder into your Home Assistant's `custom_components` directory.
3.  Restart Home Assistant.

### Configuration / Manual Setup

Once installed (via HACS or manually), you need to set up the integration:

1.  Go to **Settings** -> **Devices & Services**.
2.  Click **Add Integration** and search for **Valetudo**.

<img width="700" src="https://github.com/user-attachments/assets/8e8936e2-145c-4496-9b2b-4f35f83b0217" />
