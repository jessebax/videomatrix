# VideoMatrix

Video Matrix works on Windows only and requires 64-bit Python 3.11 or newer.
[Download Python for Windows](https://www.python.org/downloads/windows/) and
enable **Add python.exe to PATH** during installation.

## Example

![Video Matrix application screenshot](./videomatrix-example.png)

## Install and run

Open PowerShell in this folder and run:

```powershell
python -m pip install -r requirements.txt
python videomatrix.py
```

Virtual camera output requires UnityCapture to be installed separately.

NDI output requires the NDI SDK to be installed separately:
https://ndi.video/for-developers/ndi-sdk/download/
