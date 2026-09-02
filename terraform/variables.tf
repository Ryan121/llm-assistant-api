variable "docker_host" {
  description = "Docker daemon endpoint. Point at ssh://user@gpu-box to drive a remote host."
  type        = string
  default     = "unix:///var/run/docker.sock"
}

variable "network_name" {
  description = "Bridge network shared by the gateway and the model servers."
  type        = string
  default     = "llm-assistant-net"
}

variable "model_cache_volume" {
  description = "Named volume holding the Hugging Face cache. Survives `docker compose down -v`."
  type        = string
  default     = "llm-assistant-model-cache"
}

variable "model_id" {
  description = "Hugging Face repo id served by vLLM. Recorded so state reflects what is deployed."
  type        = string
  default     = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
}

variable "tensor_parallel_size" {
  description = "Number of GPUs the primary model is sharded across."
  type        = number
  default     = 2

  validation {
    condition     = var.tensor_parallel_size >= 1 && var.tensor_parallel_size <= 8
    error_message = "tensor_parallel_size must be between 1 and 8."
  }
}

variable "project_dir" {
  description = "Absolute path to this repository on the GPU host, handed to Ansible."
  type        = string
}

variable "ansible_vars_file" {
  description = "Where to render the Terraform-owned facts that Ansible consumes."
  type        = string
  default     = "../ansible/inventory/group_vars/all/terraform.yml"
}

variable "labels" {
  description = "Labels stamped onto every Docker resource this module owns."
  type        = map(string)
  default = {
    "managed-by" = "terraform"
    "project"    = "llm-assistant-api"
  }
}
