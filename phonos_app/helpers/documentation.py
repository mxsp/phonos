"""Documentation system helpers."""
from __future__ import annotations

import re

from .database import get_db


def slugify(text: str) -> str:
    """Convert text to URL-friendly slug."""
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[-\s]+', '-', text)
    return text.strip('-')


def create_search_vector(content: str) -> str:
    """Create a simplified search vector from content."""
    # Remove HTML tags
    content = re.sub(r'<[^>]+>', '', content)
    # Convert to lowercase
    content = content.lower()
    # Remove special characters
    content = re.sub(r'[^\w\s]', ' ', content)
    return content


def create_documentation_page(
    title: str,
    content: str,
    category: str,
    slug: str = None,
) -> dict:
    """Create a new documentation page."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    if not slug:
        slug = slugify(title)

    search_vector = create_search_vector(content)

    result = db.execute(
        """
        INSERT INTO documentation_pages (slug, title, content, category, search_vector)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (slug, title, content, category, search_vector),
    )

    page_id = result.lastrowid
    db.commit()

    return {
        "id": page_id,
        "slug": slug,
        "title": title,
        "content": content,
        "category": category,
        "view_count": 0,
    }


def get_documentation_page(slug: str, increment_view: bool = True) -> dict:
    """Get a documentation page by slug."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, slug, title, content, category, view_count, created_at, updated_at
        FROM documentation_pages
        WHERE slug = %s
        """,
        (slug,),
    )

    row = result.fetchone()
    if not row:
        return None

    page = dict(row)

    # Increment view count if requested
    if increment_view:
        db.execute(
            """
            UPDATE documentation_pages
            SET view_count = view_count + 1
            WHERE id = %s
            """,
            (page["id"],),
        )
        db.commit()
        page["view_count"] += 1

    return page


def get_documentation_pages_by_category(category: str) -> list[dict]:
    """Get all documentation pages in a category."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, slug, title, category, view_count, created_at, updated_at
        FROM documentation_pages
        WHERE category = %s
        ORDER BY title
        """,
        (category,),
    )

    pages = []
    for row in result.fetchall():
        pages.append(dict(row))

    return pages


def get_all_categories() -> list[str]:
    """Get list of all documentation categories."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT DISTINCT category
        FROM documentation_pages
        ORDER BY category
        """
    )

    categories = []
    for row in result.fetchall():
        categories.append(row["category"])

    return categories


def update_documentation_page(
    slug: str,
    title: str = None,
    content: str = None,
    category: str = None,
) -> bool:
    """Update a documentation page."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    updates = []
    params = []

    if title is not None:
        updates.append("title = %s")
        params.append(title)

    if content is not None:
        updates.append("content = %s")
        params.append(content)
        updates.append("search_vector = %s")
        params.append(create_search_vector(content))

    if category is not None:
        updates.append("category = %s")
        params.append(category)

    if not updates:
        return False

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(slug)

    query = f"""
        UPDATE documentation_pages
        SET {', '.join(updates)}
        WHERE slug = %s
    """

    result = db.execute(query, params)
    db.commit()

    return result.rowcount > 0


def delete_documentation_page(slug: str) -> bool:
    """Delete a documentation page."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        DELETE FROM documentation_pages
        WHERE slug = %s
        """,
        (slug,),
    )

    db.commit()
    return result.rowcount > 0


def get_popular_pages(limit: int = 10) -> list[dict]:
    """Get most viewed documentation pages."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, slug, title, category, view_count, created_at, updated_at
        FROM documentation_pages
        ORDER BY view_count DESC
        LIMIT %s
        """,
        (limit,),
    )

    pages = []
    for row in result.fetchall():
        pages.append(dict(row))

    return pages


def get_recent_pages(limit: int = 10) -> list[dict]:
    """Get recently updated documentation pages."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, slug, title, category, view_count, created_at, updated_at
        FROM documentation_pages
        ORDER BY updated_at DESC
        LIMIT %s
        """,
        (limit,),
    )

    pages = []
    for row in result.fetchall():
        pages.append(dict(row))

    return pages


# Initial documentation content
INITIAL_DOCS = [
    {
        "title": "Getting Started with Phonos",
        "category": "Getting Started",
        "content": """
# Getting Started with Phonos

Welcome to Phonos, a scientific computing platform for protein folding, genomics, and molecular modeling.

## Creating Your First Project

1. Navigate to the Dashboard
2. Click "New Project"
3. Enter a project name and description
4. Click "Create"

## Running Your First Job

1. Select a project
2. Choose a compute node (ESMFold, OpenFold, etc.)
3. Enter your sequence or upload a file
4. Click "Submit Job"

Your job will be queued and processed. You'll receive a notification when it completes.
        """
    },
    {
        "title": "Compute Nodes Overview",
        "category": "Compute Nodes",
        "content": """
# Compute Nodes Overview

Phonos provides access to various computational models for scientific research:

## Available Nodes

### ESMFold (Node 1)
Predict 3D protein structures from amino acid sequences using facebook/esmfold_v1.

### OpenFold (Node 2)
Refine protein backbones using a lightweight OpenFold-inspired surrogate.

### DeepVariant (Node 3)
Call genomic variants from long DNA sequences.

### Boltz2 (Node 4)
Sample coarse-grained energy landscapes for conformational exploration.

### ESM2 Embeddings (Node 5)
Generate residue-level and pooled protein embeddings.

### DeepChem (Node 6)
Estimate physicochemical properties from SMILES strings.

### TorchDrug (Node 7)
Generate molecular graphs and analyze connectivity.

### Aplify (Node 8)
Optimize amino acid sequences using surrogate scoring.

### Python Workbench (Node X1)
Run custom Python scripts in ephemeral Docker sandboxes.
        """
    },
    {
        "title": "Job Templates",
        "category": "Advanced Features",
        "content": """
# Job Templates

Job templates let you save and reuse job configurations across projects.

## Creating a Template

1. Configure a job with your desired parameters
2. Click "Save as Template"
3. Give your template a name and description
4. Choose whether to make it public or private

## Using a Template

1. Click "Use Template" when creating a new job
2. Select your saved template
3. Modify parameters if needed
4. Submit the job

Public templates can be shared with your team or the entire platform.
        """
    },
    {
        "title": "API Authentication",
        "category": "API",
        "content": """
# API Authentication

The Phonos API uses API keys for authentication.

## Creating an API Key

1. Go to Settings > API Keys
2. Click "Generate New Key"
3. Give your key a name
4. Select the scopes (permissions)
5. Set an expiration date (optional)
6. Copy and save your key securely

## Using Your API Key

Include your API key in the `Authorization` header:

```
Authorization: Bearer your_api_key_here
```

## Rate Limits

API keys are subject to rate limiting:
- 100 requests per minute per endpoint
- 1000 requests per hour total

Contact support for higher limits.
        """
    },
    {
        "title": "CLI Tool",
        "category": "CLI",
        "content": """
# Phonos CLI Tool

The Phonos CLI allows you to interact with the platform from your terminal.

## Installation

```bash
pip install phonos-cli
```

## Configuration

```bash
phonos config set-api-key YOUR_API_KEY
phonos config set-endpoint https://phonos.fyreflysystems.de
```

## Common Commands

### Submit a job
```bash
phonos job submit --project my-project --model type-0 --sequence "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKRQTLGQHDFSAGEGLYTHMKALRPDEDRLSPLHSVYVDQWDWERVMGDGERQFSTLKSTVEAIWAGIKATEAAVSEEFGLAPFLPDQIHFVHSQELLSRYPDLDAKGRERAIAKDLGAVFLVGIGGKLSDGHRHDVRAPDYDDWSTPSELGHAGLNGDILVWNPVLEDAFELSSMGIRVDADTLKHQLALTGDEDRLELEWHQALLRGEMPQTIGGGIGQSRLTMLLLQLPHIGQVQAGVWPAAVRESVPSLL"
```

### Check job status
```bash
phonos job status job_abc123
```

### Download results
```bash
phonos job download job_abc123 --output results.pdb
```

### List projects
```bash
phonos project list
```
        """
    }
]


def initialize_documentation() -> None:
    """Initialize documentation with default pages."""
    for doc in INITIAL_DOCS:
        try:
            create_documentation_page(
                title=doc["title"],
                content=doc["content"],
                category=doc["category"]
            )
        except Exception:
            # Page might already exist, skip
            pass
