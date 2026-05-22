# KG Query API Test Tool

AI-driven testing tool for KG Query Service (GraphQL + REST APIs) with Anthropic Claude integration for intelligent test case generation.

## Features

- **AI-Driven Test Generation**: Uses Anthropic Claude API to analyze PR changes, Jira tickets, and notes to generate smart test cases
- **Dynamic Test Scaling**: Test count adapts based on schema introspection, payload complexity, and user input (not fixed 30)
- **X-Accept-Lag Feature Testing**: Comprehensive test suites for lag-based request acceptance
- **Dual API Support**: Tests both GraphQL and REST (DRT) endpoints
- **Ground Truth Comparison**: Validates GraphQL results against REST API responses
- **Report Generation**: HTML and TXT reports with full request/response payloads
- **Live Streaming**: Real-time test progress via Server-Sent Events
- **Multi-Environment**: Supports dev-aws, dev-gcp, uat-gcp, prod-aws, custom

## Quick Start

### Prerequisites
- Python 3.9+
- pip3

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/chithra-devaraj/kg-api-tester.git
cd kg-api-tester

# 2. Install dependencies
pip3 install -r requirements.txt
```

### Running the Tool

```bash
# Start the Flask web server
python3 app.py

# Open in your browser
# http://localhost:5001
```

The tool will be available at `http://localhost:5001`

## Usage

### Web UI Configuration

1. **Select Environment**
   - Pre-configured: dev-aws, dev-gcp, uat-gcp, prod-aws
   - Or enter custom GraphQL and REST URLs

2. **Credentials**
   - Username & Password (basic auth)
   - Tenant ID (x-tenant-environment header)
   - Anthropic API Key (optional, for AI test generation)

3. **Test Configuration**
   - Asset ID, Asset Type, Relation Type
   - Target Types (comma-separated)
   - PR Details (GitHub URL for context)
   - Jira Ticket & Token (for acceptance criteria)
   - Custom Notes (additional test guidance)

4. **Run Tests**
   - Select test categories: Positive, Negative, Schema validation
   - For X-Accept-Lag testing: Select REST comparison option
   - Monitor live progress

5. **Download Reports**
   - HTML report (formatted for viewing)
   - TXT report (plain text with all details)

## API Key Setup

### Anthropic API Key (Optional)

To enable AI-driven test generation:

1. Get API key from: https://console.anthropic.com/
2. Generate a new API key
3. Enter in the tool's "Anthropic API Key" field
4. Save to environment variable for automatic loading:
   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."
   ```

## Project Structure

```
kg-api-tester/
├── app.py                 # Flask web application & session management
├── test_engine.py         # Core test execution & case generation
├── report_builder.py      # HTML/TXT report generation
├── requirements.txt       # Python dependencies
├── static/               # CSS/JS assets
└── templates/            # HTML templates
    └── index.html        # Web UI
```

## Key Components

### app.py
- Flask web server (port 5001)
- Session management (in-memory store)
- REST endpoints: `/run`, `/stream/<sid>`, `/status/<sid>`, `/download/html/<sid>`, `/download/txt/<sid>`
- Server-Sent Events streaming for live test progress

### test_engine.py
- `TestRunner`: Core test execution engine
- `RunConfig`: Test configuration dataclass
- Test case generation strategies:
  - Schema introspection-driven
  - PR change-aware
  - Jira ticket-aware
  - AI-driven (via Anthropic)
- Ground truth collection & validation
- Dynamic expected value calculation

### report_builder.py
- `build_html_report()`: Generates styled HTML reports
- `build_txt_report()`: Generates plain text reports
- Full request/response payload capture
- Acceptance criteria coverage tracking

## Testing X-Accept-Lag Feature

The tool includes comprehensive test suites for the X-Accept-Lag HTTP header feature (DEV-148086):

### Test Scenarios

1. **No Header**: Accept-all behavior (returns whatever lag state)
2. **Valid ISO-8601 Durations**: PT1M, PT5M, PT1H, PT3H, etc.
3. **Invalid Formats**: PT30S (below min), PT30 (no unit), "5minutes" (non-ISO)
4. **Negative Values**: -PT2H (invalid)
5. **Edge Cases**: Boundary values at each staleness threshold

### Staleness States Tested

- **CURRENT**: lag < 5 minutes
- **SLIGHTLY_BEHIND**: lag 5-30 minutes
- **MODERATELY_BEHIND**: lag 30 minutes - 2 hours
- **HEAVILY_BEHIND**: lag 2-4 hours
- **CRITICALLY_BEHIND**: lag ≥ 4 hours
- **UNKNOWN**: Snapshot/restore state

### Expected Behavior

- **200 OK**: Header absent OR `lag <= header_duration`
- **406 Not Acceptable**: `lag > header_duration` OR status is UNKNOWN
- **400 Bad Request**: Header below PT1M or invalid format

## Example Test Run

```bash
# Environment: dev-aws
# Asset: 019b45e7-9280-72a5-8ed8-6ffaf6fbc0a5 (BusinessTerm)
# Relation: AcronymRoleuiBusinessTerm_C
# Target Types: BusinessTerm
# Tests: X-Accept-Lag validation across REST + GraphQL
```

The tool will:
1. Generate test cases based on configuration
2. Execute tests against both APIs
3. Compare REST vs GraphQL results
4. Stream live progress to UI
5. Generate comprehensive report with all payloads

## Requirements

See `requirements.txt`:
```
flask>=3.0.0
requests>=2.31.0
anthropic>=0.20.0
```

## Support

For issues or questions about:
- **Tool Usage**: Check the web UI help or examine generated reports
- **Test Coverage**: Review test_engine.py for generation strategies
- **API Details**: See PR #1326 in knowledge-graph-services repo

## License

Internal Collibra tool

---

**Created**: 2026-05-22  
**Last Updated**: 2026-05-22  
**Status**: Production Ready
