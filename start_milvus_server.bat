@echo off
setlocal

echo [Milvus] Starting Milvus Standalone...
echo [Milvus] Data: podman volume milvus-data
echo [Milvus] API:  http://localhost:19530
echo.

podman run --rm -d ^
    -p 19530:19530 ^
    -p 9091:9091 ^
    -v milvus-data:/var/lib/milvus ^
    -e ETCD_USE_EMBED=true ^
    -e ETCD_DATA_DIR=/var/lib/milvus/etcd ^
    -e ETCD_CONFIG_PATH=/milvus/configs/embedEtcd.yaml ^
    -e COMMON_STORAGETYPE=local ^
    milvusdb/milvus:latest milvus run standalone

if %errorlevel% neq 0 (
    echo [ERROR] Failed to start. Is Podman running?
    exit /b 1
)

echo [Milvus] Server started. Logs: podman logs -f ^<container_id^>
endlocal
