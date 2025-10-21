import os
from typing import Dict

#local imports
from windows.CPU import *
from importlib import import_module

def main():
    os_info = get_os_info()
    print(f"Operating System Name: {os_info['name']}")
    print(f"Platform: {os_info['platform']}")
    call_sysinfocollection(os_info)

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
        case "win32":
            print("Collecting sysinfo for Windows...")
            cpu = import_module("windows.CPU")
            cpu.main()
        case _:
            print("Unknown or Unsupported platform")

if __name__ == "__main__":
    main()