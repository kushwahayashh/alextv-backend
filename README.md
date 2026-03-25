# alex-tv backend

Backend API for the ALEX TV app.

## Endpoints
- `GET /health` basic health check
- `GET /list?path=/media` list files/folders
- `GET /stream?path=/media/file.mkv` stream with Range support
- `GET /download-url?path=/media/file.mkv` get a direct stream URL
- `GET /terminal` web terminal UI
- `GET /favicon.ico` svg icon

## Notes
- Runs on Modal with a shared volume mounted at `/vol`.
- Media files live under `/vol/media`.
