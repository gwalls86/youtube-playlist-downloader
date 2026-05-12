# YouTube Playlist Downloader

Una herramienta profesional basada en web diseñada para centralizar el flujo de trabajo con videos de YouTube. Permite desde la descarga automatizada de playlists completas hasta el procesamiento avanzado de audio y video (Normalización EBU R128 y compresión H.265) mediante una interfaz moderna, oscura y extremadamente intuitiva.

![Versión](https://img.shields.io/badge/version-2.0-red)
![Platform](https://img.shields.io/badge/platform-Windows-blue)
![Python](https://img.shields.io/badge/python-3.8+-yellow)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 🎨 Icono del Proyecto

<p align="center">
  <img src="frontend/icon.png" width="160" alt="YouTube Manager Icon">
</p>


---

## 🎨 Interfaz Web Premium

La aplicación utiliza un backend en **FastAPI** y un frontend reactivo en **Vue 3** (sin necesidad de compilación) para ofrecer una experiencia fluida y moderna. Se ejecuta localmente y se accede a través del navegador.

---

## 🚀 Características Principales

### 1. Descarga Inteligente de Playlists
*   **Numeración Persistente**: Utiliza un sistema de `manifest.json` para asignar números fijos a cada video. Si el orden de la playlist cambia en YouTube, tus archivos locales mantienen su numeración original.
*   **Evita Duplicados**: El sistema detecta automáticamente qué videos ya han sido descargados.
*   **Gestión de Errores**: Si un video falla, se registra en un archivo de log para su revisión.

### 2. Normalización de Audio Profesional
*   **Estándar EBU R128**: Ajusta el volumen de tus videos para que todos suenen al mismo nivel, utilizando el algoritmo **loudnorm**.
*   **Sin pérdida de calidad**: El proceso solo recodifica el audio (AAC/128k por defecto) manteniendo el flujo de video intacto (stream copy).

### 3. Optimización H.265 (HEVC)
*   **Reducción de Espacio**: Convierte tus videos a H.265, reduciendo el tamaño del archivo hasta en un 50-70% manteniendo una calidad visual excepcional.
*   **Control Total**: Ajusta el **CRF** (Constant Rate Factor) y el **Preset** de velocidad directamente desde la interfaz.

---

## 🛠️ Requisitos del Sistema

Esta herramienta está optimizada para **Windows 10/11** y requiere:

1.  **Python 3.8 o superior**.
2.  **Dependencias de Python**: `fastapi`, `uvicorn`.
3.  **Herramientas externas**:
    *   **yt-dlp.exe** (Motor de descarga).
    *   **FFmpeg** (Suite de procesamiento multimedia).

*(Nota: Puedes colocar estas herramientas en la carpeta del proyecto y configurar sus rutas en la sección "Herramientas" de la interfaz).*

---

## 📦 Guía de Uso

### 1. Instalar Dependencias
Abre una terminal en la carpeta del proyecto e instala las librerías necesarias de Python:

```powershell
pip install fastapi uvicorn
```

### 2. Iniciar la Aplicación
Simplemente ejecuta el archivo automatizado en la raíz del proyecto:

```powershell
.\start.bat
```

Esto hará lo siguiente:
1.  Iniciará el backend de FastAPI en segundo plano (puerto `8005`).
2.  Abrirá automáticamente tu navegador en `http://localhost:8005`.

### 3. Configuración Inicial (Importante)
La primera vez que abras la aplicación, ve a la sección **Herramientas** en la parte inferior de la barra lateral y verifica las rutas. 

Si has colocado las herramientas en la raíz del proyecto (como se sugiere), las rutas relativas que debes ingresar son:
*   **ffmpeg**: `..\0-FFmpeg\bin\ffmpeg.exe`
*   **yt-dlp**: `..\yt-dlp.exe`

*(Estas rutas son relativas a la carpeta `backend` desde donde se ejecuta el servidor).*


---

## 📝 Créditos y Versión
- **Versión**: 2.0 (Migración a Web UI)
- **Motor de Descarga**: [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- **Motor de Procesamiento**: [FFmpeg](https://ffmpeg.org/)

---
*Desarrollado por **gwalls86***
