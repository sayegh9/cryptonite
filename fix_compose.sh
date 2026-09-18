sed -i '139d' infra/docker-compose.yml
sed -i '/- \.\.\/data:\/app\/data:rw/a\      - ../services/api:/app/services/api\n      - ../packages/agents:/app/packages/agents' infra/docker-compose.yml
