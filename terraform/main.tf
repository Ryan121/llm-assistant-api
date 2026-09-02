# -----------------------------------------------------------------------------
# Terraform owns the *durable* Docker resources; Compose owns the containers.
#
# The split matters for one specific reason: the model cache is 60+ GB and
# takes half an hour to refill. Keeping it in Terraform state, declared
# `external` in Compose, means no `docker compose down -v` can wipe it, and
# `terraform destroy` is the only thing that will.
# -----------------------------------------------------------------------------

resource "docker_network" "llm" {
  name   = var.network_name
  driver = "bridge"

  dynamic "labels" {
    for_each = var.labels
    content {
      label = labels.key
      value = labels.value
    }
  }
}

resource "docker_volume" "model_cache" {
  name = var.model_cache_volume

  dynamic "labels" {
    for_each = var.labels
    content {
      label = labels.key
      value = labels.value
    }
  }
}

# Facts Ansible needs but should not have to re-derive from .env.
resource "local_file" "ansible_vars" {
  filename        = var.ansible_vars_file
  file_permission = "0644"

  content = yamlencode({
    "_generated_by"        = "terraform - do not edit by hand"
    "docker_network"       = docker_network.llm.name
    "model_cache_volume"   = docker_volume.model_cache.name
    "model_cache_path"     = docker_volume.model_cache.mountpoint
    "project_dir"          = var.project_dir
    "model_id"             = var.model_id
    "tensor_parallel_size" = var.tensor_parallel_size
  })
}
