# Hound MCP for Home Assistant

This integration connects Home Assistant to [hound-mcp](https://github.com/dondai1234/master-fetch) for web research capabilities.

## Installation

### 1. Install Hound MCP (Docker recommended)

```bash
git clone https://github.com/imonlinux/master-fetch.git
cd master-fetch
docker-compose up -d
```

Hound will be available at `http://localhost:8765/mcp`

### 2. Install the Home Assistant Integration

Copy the `home-assistant` folder to your Home Assistant custom integrations:

```bash
# For HA Container/Supervised
cp -r home-assistant /homeassistant/custom_components/hound_mcp

# For HA Core
cp -r home-assistant ~/.homeassistant/custom_components/hound_mcp

# Restart Home Assistant
```

### 3. Configure the Integration

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for "Hound MCP"
3. Enter:
   - **Name**: Hound MCP (or any name you prefer)
   - **URL**: `http://hound` (if using Docker Compose on same network) or `http://localhost`
   - **Port**: `8765`

## Usage

### Sensors

- **Hound MCP Status**: Shows connection status (connected/disconnected)
- **Hound MCP Version**: Shows installed hound version

### Services

The integration exposes the following services that can be used in automations:

#### `hound_mcp.fetch_webpage`
Fetch a webpage and extract content.

```yaml
service: hound_mcp.fetch_webpage
data:
  url: https://example.com
  use_browser: false
  extract: article
```

#### `hound_mcp.web_search`
Search the web using hound's keyless metasearch.

```yaml
service: hound_mcp.web_search
data:
  query: latest home assistant release
  num_results: 10
```

#### `hound_mcp.crawl_website`
Crawl a website starting from a URL.

```yaml
service: hound_mcp.crawl_website
data:
  url: https://example.com
  max_pages: 10
```

#### `hound_mcp.take_screenshot`
Take a screenshot of a webpage.

```yaml
service: hound_mcp.take_screenshot
data:
  url: https://example.com
  width: 1920
  height: 1080
```

## Example Automations

### Weather Research Automation

```yaml
alias: "Weather Research"
description: "Research weather conditions using hound"
trigger:
  - platform: time
    at: "07:00:00"
action:
  - service: hound_mcp.web_search
    data:
      query: "weather forecast {{ states('input_text.city') }}"
      num_results: 5
    response_variable: search_results
  - service: notify.mobile_app
    data:
      message: "{{ search_results.summary }}"
```

### News Headline Fetch

```yaml
alias: "Daily News Summary"
trigger:
  - platform: time
    at: "08:00:00"
action:
  - service: hound_mcp.fetch_webpage
    data:
      url: "https://news.example.com"
      extract: article
    response_variable: news_data
  - service: input_text.set
    target:
      entity_id: input_text.daily_news
    data:
      value: "{{ news_data.content }}"
```

## Development

The hound-mcp fork is maintained at: https://github.com/imonlinux/master-fetch

Original project: https://github.com/dondai1234/master-fetch