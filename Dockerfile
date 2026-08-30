# Start from a small, official Python image rather than a full OS -
# this keeps the final image smaller and faster to build/pull.
FROM python:3.12-slim

# All following commands run from this directory inside the container.
WORKDIR /app

# Copy just the requirements file first (not the whole project yet).
# Docker caches each step - if requirements.txt hasn't changed, it can
# reuse this step on future builds instead of reinstalling everything.
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Now copy the rest of the project (app.py, templates/, static/) in.
COPY . .

# Documents that the container listens on port 5000.
# This doesn't actually publish the port - that happens with `docker run -p`.
EXPOSE 5000

# The command that runs when the container starts.
CMD ["python", "app.py"]