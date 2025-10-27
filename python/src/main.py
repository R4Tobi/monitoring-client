import os
import requests
import time
from typing import Dict

#local imports
from importlib import import_module
from windows.windows import Windows
from linux.linux import Linux

def main():
    os_info = get_os_info()
    print(f"Operating System Name: {os_info['name']}")
    print(f"Platform: {os_info['platform']}")

    endpoint = os.getenv("ENDPOINT_URL", "http://localhost:8080/hosts")
    while True:
        info = call_sysinfocollection(os_info)
        print(info)
        response = requests.post(endpoint, json=info)
        if response.status_code == 200:
            print("Sysinfo successfully sent to the server.")
        else:
            print(f"Failed to send sysinfo. Status code: {response.status_code}")

def get_os_info():
    return {
        "name": os.name,
        "platform": os.sys.platform
    }

def call_sysinfocollection(os_info: Dict) -> None:
    # Placeholder for future implementation
    match os_info['platform']:
        case "linux":
            print("Collecting sysinfo for Linux...")
            return Linux().gather_info()
        case "win32":
            print("Collecting sysinfo for Windows...")
            return Windows().gather_info()
        case _:
            print("Unknown or Unsupported platform")

if __name__ == "__main__":
    main()