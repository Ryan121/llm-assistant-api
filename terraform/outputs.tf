output "network_name" {
  description = "Bridge network Compose attaches to."
  value       = docker_network.llm.name
}

output "model_cache_volume" {
  description = "Named volume holding the Hugging Face weights."
  value       = docker_volume.model_cache.name
}

output "model_cache_path" {
  description = "Host path of the model cache, for `du -sh` and disk alarms."
  value       = docker_volume.model_cache.mountpoint
}

output "model_id" {
  description = "Model this deployment is configured to serve."
  value       = var.model_id
}
