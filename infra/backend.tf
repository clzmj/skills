terraform {
  backend "s3" {
    bucket         = "clzmj-skills-tfstate"
    key            = "skills/terraform.tfstate"
    region         = "auto"
    use_lockfile   = true
    use_path_style = true

    # AWS_ENDPOINT_URL_S3 selects this project's Cloudflare account endpoint.
    # Credentials are AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, never HCL.
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_s3_checksum            = true
  }
}
