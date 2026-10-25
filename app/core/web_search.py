"""
Web Search Module - Internet search functionality for the bot
Supports multiple search providers: Serper, SerpAPI, Bing, DuckDuckGo
"""
import logging
from dataclasses import dataclass
from typing import List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Single search result"""
    title: str
    link: str
    snippet: str
    source: str = ""


class WebSearchClient:
    """Web search client with multiple provider support"""
    
    def __init__(self):
        self.providers = []
        self._setup_providers()
    
    def _setup_providers(self):
        """Initialize available search providers"""
        # Serper.dev (Google Search) - preferred, fast & cheap
        if hasattr(settings, 'SERPER_API_KEY') and settings.SERPER_API_KEY:
            self.providers.append('serper')
        
        # SerpAPI (Google Search)
        if hasattr(settings, 'SERPAPI_KEY') and settings.SERPAPI_KEY:
            self.providers.append('serpapi')
        
        # Bing Search API
        if hasattr(settings, 'BING_API_KEY') and settings.BING_API_KEY:
            self.providers.append('bing')
        
        # DuckDuckGo (free, no API key needed)
        self.providers.append('duckduckgo')
        
        logger.info(f"Web search providers initialized: {self.providers}")
    
    async def search(
        self,
        query: str,
        num_results: int = 5,
        provider: Optional[str] = None
    ) -> List[SearchResult]:
        """
        Perform web search
        
        Args:
            query: Search query
            num_results: Number of results to return
            provider: Specific provider to use (auto-select if None)
            
        Returns:
            List of search results
        """
        if provider:
            # Use specific provider
            if provider == 'serper':
                return await self._search_serper(query, num_results)
            elif provider == 'serpapi':
                return await self._search_serpapi(query, num_results)
            elif provider == 'bing':
                return await self._search_bing(query, num_results)
            elif provider == 'duckduckgo':
                return await self._search_duckduckgo(query, num_results)
        
        # Auto-select provider based on availability
        for prov in self.providers:
            try:
                if prov == 'serper':
                    return await self._search_serper(query, num_results)
                elif prov == 'serpapi':
                    return await self._search_serpapi(query, num_results)
                elif prov == 'bing':
                    return await self._search_bing(query, num_results)
                elif prov == 'duckduckgo':
                    return await self._search_duckduckgo(query, num_results)
            except Exception as e:
                logger.warning(f"Provider {prov} failed: {e}")
                continue
        
            logger.error("All search providers failed")
        return []
    
    async def _search_serper(self, query: str, num_results: int = 5) -> List[SearchResult]:
        """Search using Serper.dev (Google Search API)"""
        if not hasattr(settings, 'SERPER_API_KEY') or not settings.SERPER_API_KEY:
            raise ValueError("SERPER_API_KEY not configured")
        
        url = "https://google.serper.dev/search"
        headers = {
            "X-API-KEY": settings.SERPER_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "q": query,
            "num": num_results
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload, timeout=30.0)
            response.raise_for_status()
            data = response.json()
            
            results = []
            
            # Extract organic results
            if 'organic' in data:
                for item in data['organic'][:num_results]:
                    results.append(SearchResult(
                        title=item.get('title', ''),
                        link=item.get('link', ''),
                        snippet=item.get('snippet', ''),
                        source='Google'
                    ))
            
            # Add answer box if available
            if 'answerBox' in data:
                answer = data['answerBox']
                results.insert(0, SearchResult(
                    title=answer.get('title', 'Direct Answer'),
                    link=answer.get('link', ''),
                    snippet=answer.get('answer', answer.get('snippet', '')),
                    source='Google Answer'
                ))
            
            # Add knowledge graph if available
            if 'knowledgeGraph' in data and not results:
                kg = data['knowledgeGraph']
                results.append(SearchResult(
                    title=kg.get('title', 'Knowledge Graph'),
                    link=kg.get('descriptionLink', ''),
                    snippet=kg.get('description', ''),
                    source='Knowledge Graph'
                ))
            
            logger.info(f"Serper search returned {len(results)} results for: {query[:50]}...")
            return results
    
    async def _search_serpapi(self, query: str, num_results: int = 5) -> List[SearchResult]:
        """Search using SerpAPI (Google)"""
        if not hasattr(settings, 'SERPAPI_KEY') or not settings.SERPAPI_KEY:
            raise ValueError("SERPAPI_KEY not configured")
        
        url = "https://serpapi.com/search"
        params = {
            "q": query,
            "api_key": settings.SERPAPI_KEY,
            "engine": "google",
            "num": num_results,
            "output": "json"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=30.0)
            response.raise_for_status()
            data = response.json()
            
            results = []
            
            # Extract organic results
            if 'organic_results' in data:
                for item in data['organic_results'][:num_results]:
                    results.append(SearchResult(
                        title=item.get('title', ''),
                        link=item.get('link', ''),
                        snippet=item.get('snippet', ''),
                        source='Google'
                    ))
            
            # Add answer box if available
            if 'answer_box' in data:
                answer = data['answer_box']
                results.insert(0, SearchResult(
                    title=answer.get('title', 'Direct Answer'),
                    link=answer.get('link', ''),
                    snippet=answer.get('answer', answer.get('snippet', '')),
                    source='Google Answer'
                ))
            
            logger.info(f"SerpAPI search returned {len(results)} results for: {query[:50]}...")
            return results
    
    async def _search_bing(self, query: str, num_results: int = 5) -> List[SearchResult]:
        """Search using Bing API"""
        if not hasattr(settings, 'BING_API_KEY') or not settings.BING_API_KEY:
            raise ValueError("BING_API_KEY not configured")
        
        url = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": settings.BING_API_KEY}
        params = {
            "q": query,
            "count": num_results,
            "textDecorations": False,
            "textFormat": "HTML"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params, timeout=30.0)
            response.raise_for_status()
            data = response.json()
            
            results = []
            
            if 'webPages' in data and 'value' in data['webPages']:
                for item in data['webPages']['value'][:num_results]:
                    results.append(SearchResult(
                        title=item.get('name', ''),
                        link=item.get('url', ''),
                        snippet=item.get('snippet', ''),
                        source='Bing'
                    ))
            
            logger.info(f"Bing search returned {len(results)} results for: {query[:50]}...")
            return results
    
    async def _search_duckduckgo(self, query: str, num_results: int = 5) -> List[SearchResult]:
        """Search using DuckDuckGo (HTML scraping)"""
        from bs4 import BeautifulSoup
        
        url = "https://html.duckduckgo.com/html/"
        params = {"q": query}
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, data=params, timeout=30.0)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            results = []
            
            # Parse results
            for result in soup.find_all('div', class_='result')[:num_results]:
                title_elem = result.find('a', class_='result__a')
                snippet_elem = result.find('a', class_='result__snippet')
                
                if title_elem and snippet_elem:
                    results.append(SearchResult(
                        title=title_elem.get_text(strip=True),
                        link=title_elem.get('href', ''),
                        snippet=snippet_elem.get_text(strip=True),
                        source='DuckDuckGo'
                    ))
            
            logger.info(f"DuckDuckGo search returned {len(results)} results for: {query[:50]}...")
            return results
    
    def format_results(self, results: List[SearchResult], max_length: int = 2000) -> str:
        """Format search results for LLM consumption"""
        if not results:
            return "Поиск в интернете не дал результатов."
        
        formatted = "Результаты поиска в интернете:\n\n"
        
        for i, result in enumerate(results, 1):
            entry = f"{i}. {result.title}\n"
            entry += f"   Источник: {result.source}\n"
            entry += f"   {result.snippet}\n"
            entry += f"   URL: {result.link}\n\n"
            
            if len(formatted) + len(entry) > max_length:
                formatted += f"...и еще {len(results) - i + 1} результатов"
                break
            
            formatted += entry
        
        return formatted.strip()
    
    async def search_and_format(self, query: str, num_results: int = 3) -> str:
        """Search and return formatted results"""
        results = await self.search(query, num_results)
        return self.format_results(results)


# Global web search client instance
web_search = WebSearchClient()
