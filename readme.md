
python3 -m venv venv

source venv/bin/activate

pip install -r requirements.txt
||
pip install flask flask-cors python-dotenv spotipy


cd playlist-mover-server
docker build -t playlist-mover-server-api:latest .
docker run -d \
  --name playlist-mover-server-api-1 \
  --network playlist-mover-server_default \
ls  -p 5000:5000 \
  --env-file .env \
  playlist-mover-server-api:latest



docker stop playlist-mover-server-api-1 && docker rm playlist-mover-server-api-1                                                playlist-mover-server  base 17:29:26
docker build -t playlist-mover-server-api:latest .
docker run -d --name playlist-mover-server-api-1 \
  --network playlist-mover-server_default \
  -p 5000:5000 --env-file .env \
  playlist-mover-server-api:latest
playlist-mover-server-api-1