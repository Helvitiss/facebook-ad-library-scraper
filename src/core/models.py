from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

@dataclass
class GraphQLPage:
    """Модель страницы с результатами GraphQL запроса."""
    cursor: Optional[str] = None
    raw_creatives: list = field(default_factory=list)
    variables: Optional[dict] = None
    doc_id: Optional[str] = None
    cookies: Dict[str, str] = field(default_factory=dict)
    lsd: Optional[str] = None

@dataclass
class Creative:
    """Модель отдельного рекламного креатива."""
    ad_archive_id: str
    video_urls: List[str] = field(default_factory=list)
    image_urls: List[str] = field(default_factory=list)
    text: Optional[str] = None
    transparency_data: Optional[Dict[str, Any]] = None
    start_date: Optional[int] = None # Unix timestamp

@dataclass
class AdGroup:
    """Группа объявлений (collation), объединенная общим смыслом или визуалом."""
    collation_id: Optional[str] = None
    link_url: Optional[str] = None
    creatives: List[Creative] = field(default_factory=list)
    total_reaches: Optional[Dict[str, Any]] = None
    rsoc_keywords: List[str] = field(default_factory=list)
