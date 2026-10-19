# Запуск isolated runner против сервера на хосте:

# docker run --rm \
#   -v <host>/.workspaces:/workspaces \
#   -v <host>/state/task-contracts:/run/task-contracts \
#   aip-runner:latest python /app/runner.py --api http://host.docker.internal:7700 \
#     --uid <user-id> --change <change-id> [--task <task-id>] [--execute-opencode]
