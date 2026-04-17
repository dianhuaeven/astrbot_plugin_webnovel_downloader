from .base import Extractor
from .fallback import FallbackRuleExtractor
from .profiled import ProfiledExtractor
from .template_novelfull import NovelFullLikeExtractor
from .template_novelpub import NovelPubLikeExtractor
from .template_wordpress import WordpressMadaraLikeExtractor

__all__ = [
    "Extractor",
    "FallbackRuleExtractor",
    "ProfiledExtractor",
    "WordpressMadaraLikeExtractor",
    "NovelFullLikeExtractor",
    "NovelPubLikeExtractor",
]
