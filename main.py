import os
import uuid
import asyncio
import aiohttp
import logging
import logging.handlers
import re
import time
import json
import signal
from datetime import datetime, timedelta
from pytz import timezone
from fastapi import FastAPI, Request, HTTPException, Depends, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any, Union, List, Tuple, Callable, TypeVar, ParamSpec
from contextlib import asynccontextmanager
from pydantic import BaseModel, validator, Field
from functools import wraps
from prometheus_client import Counter, Histogram
from pydantic_settings import BaseSettings

# Type variables for type hints
P = ParamSpec('P')
T = TypeVar('T')

# Prometheus metrics
TRADE_REQUESTS = Counter('trade_requests', 'Total trade requests')
TRADE_LATENCY = Histogram('trade_latency', 'Trade processing latency')

##############################################################################
# Error Handling Infrastructure
##############################################################################

class TradingError(Exception):
    """Base exception for trading-related errors"""
    pass

class MarketError(TradingError):
    """Errors related to market conditions"""
    pass

class OrderError(TradingError):
    """Errors related to order execution"""
    pass

class CustomValidationError(TradingError):
    """Errors related to data validation"""
    pass

def handle_async_errors(func: Callable[P, T]) -> Callable[P, T]:
    """
    Decorator for handling errors in async functions.
    Logs errors and maintains proper error propagation.
    """
    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await func(*args, **kwargs)
        except TradingError as e:
            logger.error(f"Trading error in {func.__name__}: {str(e)}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Unexpected error in {func.__name__}: {str(e)}", exc_info=True)
            raise TradingError(f"Internal error in {func.__name__}: {str(e)}") from e
    return wrapper

def handle_sync_errors(func: Callable[P, T]) -> Callable[P, T]:
    """
    Decorator for handling errors in synchronous functions.
    Similar to handle_async_errors but for sync functions.
    """
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return func(*args, **kwargs)
        except TradingError as e:
            logger.error(f"Trading error in {func.__name__}: {str(e)}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Unexpected error in {func.__name__}: {str(e)}", exc_info=True)
            raise TradingError(f"Internal error in {func.__name__}: {str(e)}") from e
    return wrapper

##############################################################################
# Configuration & Constants
##############################################################################

class Settings(BaseSettings):
    """Centralized configuration management"""
    oanda_account: str = Field(default="", description="Oanda account ID")
    oanda_token: str = Field(default="", description="Oanda API token")
    oanda_api_url: str = Field(
        default="https://api-fxtrade.oanda.com/v3",
        description="Oanda API URL"
    )
    oanda_environment: str = Field(
        default="practice",
        description="Oanda environment (practice or live)"
    )
    
    allowed_origins: List[str] = Field(
        default=["*"], 
        description="CORS allowed origins"
    )
    
    connect_timeout: int = Field(
        default=10, 
        description="Connection timeout in seconds"
    )
    read_timeout: int = Field(
        default=30, 
        description="Read timeout in seconds"
    )
    total_timeout: int = Field(
        default=45, 
        description="Total request timeout in seconds"
    )
    
    max_simultaneous_connections: int = Field(
        default=100, 
        description="Maximum simultaneous connections"
    )
    
    # Trading parameters
    risk_percentage: float = Field(
        default=2.0, 
        description="Risk percentage per trade (0-100)"
    )
    max_daily_loss: float = Field(
        default=10.0, 
        description="Maximum daily loss percentage (0-100)"
    )
    
    max_retries: int = Field(
        default=3, 
        description="Maximum retry attempts for API calls"
    )
    base_delay: float = Field(
        default=1.0, 
        description="Base delay for retry backoff in seconds"
    )
    
    # BOS signal parameters
    bos_enable: bool = Field(
        default=True, 
        description="Enable BOS signal processing"
    )
    choch_enable: bool = Field(
        default=False, 
        description="Enable CHoCH signal processing"
    )
    
    # Set log level
    log_level: str = Field(
        default="INFO", 
        description="Logging level"
    )
    
    # Redis URL for optional caching/state
    redis_url: Optional[str] = Field(
        default=None, 
        description="Redis URL for optional state management"
    )
    
    # Time zone for logging
    timezone: str = Field(
        default="UTC", 
        description="Timezone for date/time operations"
    )
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

# Initialize configuration
config = Settings()

# Session Configuration
HTTP_REQUEST_TIMEOUT = aiohttp.ClientTimeout(
    total=config.total_timeout,
    connect=config.connect_timeout,
    sock_read=config.read_timeout
)

# Instrument settings
INSTRUMENT_LEVERAGES = {
    # Forex major pairs
    "EUR_USD": 30, "GBP_USD": 30, "USD_JPY": 30, "USD_CHF": 30,
    "USD_CAD": 30, "AUD_USD": 30, "NZD_USD": 30,
    
    # Forex minor pairs
    "EUR_GBP": 20, "EUR_JPY": 20, "GBP_JPY": 20, "AUD_JPY": 20,
    "EUR_AUD": 20, "EUR_CAD": 20, "USD_SGD": 20, "USD_HKD": 20,
    
    # Metals
    "XAU_USD": 20, "XAG_USD": 20,
    
    # Indices
    "US30_USD": 20, "SPX500_USD": 20, "NAS100_USD": 20,
    "UK100_GBP": 20, "EU50_EUR": 20, "JP225_USD": 20,
    
    # Crypto (lower leverage)
    "BTC_USD": 2, "ETH_USD": 2, "LTC_USD": 2, "BCH_USD": 2,
}

# TradingView Field Mapping
TV_FIELD_MAP = {
    'symbol': 'symbol',
    'action': 'action',
    'timeframe': 'timeframe',
    'orderType': 'orderType',
    'timeInForce': 'timeInForce',
    'percentage': 'percentage',
    'price': 'price',
    'stopLoss': 'stopLoss',
    'takeProfit': 'takeProfit',
    'signalType': 'signalType',  # For BOS, CHoCH identification
    'comment': 'comment'
}

# Error Mapping - code: (is_fatal, message, http_status)
ERROR_MAP = {
    "INSUFFICIENT_MARGIN": (True, "Insufficient margin", 400),
    "ACCOUNT_NOT_TRADEABLE": (True, "Account restricted", 403),
    "MARKET_HALTED": (False, "Market is halted", 503),
    "RATE_LIMIT": (True, "Rate limit exceeded", 429)
}

##############################################################################
# Logging Setup
##############################################################################

class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging"""
    def format(self, record):
        return json.dumps({
            "timestamp": datetime.now(timezone(config.timezone)).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, 'request_id', None),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno
        })

def setup_logging():
    """Setup logging with improved error handling and rotation"""
    try:
        # Try to create logs directory on Render
        log_dir = '/opt/render/project/src/logs'
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, 'trading_bot.log')
    except Exception as e:
        # Fallback to local directory
        log_file = 'trading_bot.log'
        print(f"Using default log file location due to error: {str(e)}")

    formatter = JSONFormatter()
    
    # Configure file handler with proper encoding and rotation
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # Clear existing handlers
    root_logger = logging.getLogger()
    for hdlr in root_logger.handlers[:]:
        root_logger.removeHandler(hdlr)
    
    # Set log level from config
    log_level = getattr(logging, config.log_level)
    root_logger.setLevel(log_level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    return logging.getLogger('trading_bot')

logger = setup_logging()

##############################################################################
# Models
##############################################################################

class AlertData(BaseModel):
    """Alert data model with improved validation"""
    symbol: str
    action: str
    signalType: Optional[str] = None  # 'BOS' or 'CHoCH'
    timeframe: Optional[str] = "1M"
    orderType: Optional[str] = "MARKET"
    timeInForce: Optional[str] = "FOK"
    percentage: Optional[float] = None
    comment: Optional[str] = None

    @validator('symbol')
    def validate_symbol(cls, v):
        """Validate and normalize instrument symbol"""
        if not v:
            raise ValueError("Symbol cannot be empty")
        
        # Handle various formats
        v = v.upper().replace('/', '_').replace('-', '_')
        
        # Common symbol mappings
        mappings = {
            'XAUUSD': 'XAU_USD',
            'EURUSD': 'EUR_USD',
            'GBPUSD': 'GBP_USD',
            'USDJPY': 'USD_JPY',
            'BTCUSD': 'BTC_USD',
        }
        
        if v in mappings:
            return mappings[v]
        
        # Try to form proper instrument name if not in mappings
        if '_' not in v and len(v) >= 6:
            # Separate currency pairs properly (EURUSD -> EUR_USD)
            return f"{v[:3]}_{v[3:]}"
        
        return v

    @validator('action')
    def validate_action(cls, v):
        """Validate action with strict checking"""
        valid_actions = ['BUY', 'SELL', 'CLOSE', 'BUY_BOS', 'SELL_BOS', 
                         'BUY_CHOCH', 'SELL_CHOCH']
        v = v.upper()
        if v not in valid_actions:
            raise ValueError(f"Action must be one of {valid_actions}")
        return v
    
    @validator('signalType')
    def validate_signal_type(cls, v, values):
        """Validate signal type against action"""
        if v is None:
            # Infer signalType from action if possible
            action = values.get('action', '')
            if '_BOS' in action:
                return 'BOS'
            elif '_CHOCH' in action:
                return 'CHoCH'
            return None
        
        valid_signals = ['BOS', 'CHOCH', 'FVG', 'OB']
        v = v.upper()
        if v not in valid_signals:
            raise ValueError(f"Signal type must be one of {valid_signals}")
        return v

    @validator('timeframe')
    def validate_timeframe(cls, v):
        """Validate timeframe format"""
        if v is None:
            return "1M"
            
        if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
            v = f"{v}M"  # Assume minutes if only a number
            
        # Parse the timeframe
        if isinstance(v, str):
            v = v.upper()
            if re.match(r'^\d+[MHD]$', v):
                return v
        
        raise ValueError(f"Invalid timeframe format: {v}. Use format like '15M', '1H', '1D'")

    @validator('percentage')
    def validate_percentage(cls, v):
        """Validate percentage within bounds"""
        if v is None:
            return config.risk_percentage
            
        if not 0 < v <= 100:
            raise ValueError("Percentage must be between 0 and 100")
        return v

    class Config:
        """Pydantic config"""
        extra = "ignore"  # Allow extra fields
        arbitrary_types_allowed = True

class TradeResponse(BaseModel):
    """Response model for trade execution"""
    success: bool
    message: str
    request_id: str
    trade_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

##############################################################################
# Session Management
##############################################################################

_session: Optional[aiohttp.ClientSession] = None

async def get_session(force_new: bool = False) -> aiohttp.ClientSession:
    """Get or create a session with improved error handling"""
    global _session
    try:
        if _session is None or _session.closed or force_new:
            if _session and not _session.closed:
                await _session.close()
            
            _session = aiohttp.ClientSession(
                timeout=HTTP_REQUEST_TIMEOUT,
                headers={
                    "Authorization": f"Bearer {config.oanda_token}",
                    "Content-Type": "application/json",
                    "Accept-Datetime-Format": "RFC3339"
                }
            )
        return _session
    except Exception as e:
        logger.error(f"Session creation error: {str(e)}")
        raise

async def cleanup_sessions():
    """Cleanup sessions when shutting down"""
    global _session
    if _session and not _session.closed:
        logger.info("Closing HTTP session")
        await _session.close()
        _session = None

##############################################################################
# Market Utilities
##############################################################################

@handle_async_errors
async def get_current_price(instrument: str, action: str) -> float:
    """Get current market price for an instrument"""
    try:
        session = await get_session()
        url = f"{config.oanda_api_url}/accounts/{config.oanda_account}/pricing"
        params = {"instruments": instrument}
        
        async with session.get(url, params=params) as response:
            if response.status != 200:
                error_text = await response.text()
                raise ValueError(f"Price fetch failed: {error_text}")
                
            data = await response.json()
            if not data.get('prices'):
                raise ValueError(f"No price data received for {instrument}")
                
            bid = float(data['prices'][0]['bids'][0]['price'])
            ask = float(data['prices'][0]['asks'][0]['price'])
            
            # Return appropriate price based on action
            if action in ['BUY', 'BUY_BOS', 'BUY_CHOCH']:
                return ask
            return bid
    except Exception as e:
        logger.error(f"Error getting price for {instrument}: {str(e)}")
        raise

@handle_async_errors
async def get_account_summary() -> Dict[str, Any]:
    """Get account summary including balance and positions"""
    try:
        session = await get_session()
        url = f"{config.oanda_api_url}/accounts/{config.oanda_account}/summary"
        
        async with session.get(url) as response:
            if response.status != 200:
                error_text = await response.text()
                raise ValueError(f"Account summary fetch failed: {error_text}")
                
            return await response.json()
    except Exception as e:
        logger.error(f"Error getting account summary: {str(e)}")
        raise

@handle_async_errors
async def get_open_positions() -> Dict[str, Any]:
    """Get all open positions for the account"""
    try:
        session = await get_session()
        url = f"{config.oanda_api_url}/accounts/{config.oanda_account}/openPositions"
        
        async with session.get(url) as response:
            if response.status != 200:
                error_text = await response.text()
                raise ValueError(f"Open positions fetch failed: {error_text}")
                
            return await response.json()
    except Exception as e:
        logger.error(f"Error getting open positions: {str(e)}")
        raise

##############################################################################
# Trade Execution
##############################################################################

@handle_async_errors
async def calculate_position_size(instrument: str, risk_percentage: float) -> int:
    """Calculate position size based on risk percentage"""
    try:
        # Get account summary to calculate position size
        account_summary = await get_account_summary()
        balance = float(account_summary['account']['balance'])
        
        # Calculate risk amount
        risk_amount = balance * (risk_percentage / 100)
        
        # Get leverage for the instrument
        leverage = INSTRUMENT_LEVERAGES.get(instrument, 20)  # Default to 20:1
        
        # Calculate position size
        if instrument.startswith('XAU_'):
            # Gold is quoted differently (per oz)
            price = await get_current_price(instrument, 'BUY')
            units = int((risk_amount * leverage) / price * 100)  # Smallest increment is 0.01 oz
            return units
        elif any(crypto in instrument for crypto in ['BTC_', 'ETH_', 'LTC_']):
            # Crypto position sizing
            price = await get_current_price(instrument, 'BUY')
            units = int((risk_amount * leverage) / price * 100000)  # Small increments for crypto
            return units
        else:
            # Standard forex position sizing
            units = int(risk_amount * leverage * 100)  # Standard lot size adjustments
            return units
    except Exception as e:
        logger.error(f"Error calculating position size: {str(e)}")
        raise

@handle_async_errors
async def execute_market_order(
    instrument: str, 
    units: int
) -> Dict[str, Any]:
    """Execute a market order without SL/TP"""
    try:
        session = await get_session()
        url = f"{config.oanda_api_url}/accounts/{config.oanda_account}/orders"
        
        # Build order request
        order_data = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT"
            }
        }
            
        logger.info(f"Executing order: {json.dumps(order_data)}")
        
        async with session.post(url, json=order_data) as response:
            response_data = await response.json()
            
            if response.status != 201:
                logger.error(f"Order execution failed: {json.dumps(response_data)}")
                error_message = response_data.get('errorMessage', 'Unknown error')
                raise OrderError(f"Order execution failed: {error_message}")
                
            logger.info(f"Order executed successfully: {json.dumps(response_data)}")
            return response_data
    except Exception as e:
        logger.error(f"Error executing market order: {str(e)}")
        raise

@handle_async_errors
async def close_position(instrument: str) -> Dict[str, Any]:
    """Close an open position for an instrument"""
    try:
        session = await get_session()
        url = f"{config.oanda_api_url}/accounts/{config.oanda_account}/positions/{instrument}/close"
        
        # Get position details first to determine units
        positions = await get_open_positions()
        target_position = None
        
        for position in positions.get('positions', []):
            if position['instrument'] == instrument:
                target_position = position
                break
                
        if not target_position:
            raise OrderError(f"No open position found for {instrument}")
            
        # Determine which units to close
        close_data = {}
        
        if float(target_position.get('long', {}).get('units', 0)) > 0:
            close_data["longUnits"] = "ALL"
        
        if float(target_position.get('short', {}).get('units', 0)) < 0:
            close_data["shortUnits"] = "ALL"
            
        if not close_data:
            raise OrderError(f"No units to close for {instrument}")
            
        logger.info(f"Closing position for {instrument}: {json.dumps(close_data)}")
        
        async with session.put(url, json=close_data) as response:
            response_data = await response.json()
            
            if response.status != 200:
                logger.error(f"Position close failed: {json.dumps(response_data)}")
                error_message = response_data.get('errorMessage', 'Unknown error')
                raise OrderError(f"Position close failed: {error_message}")
                
            logger.info(f"Position closed successfully: {json.dumps(response_data)}")
            return response_data
    except Exception as e:
        logger.error(f"Error closing position: {str(e)}")
        raise

##############################################################################
# Alert Handler
##############################################################################

class AlertHandler:
    """Handler for processing trading alerts"""
    
    def __init__(self):
        self.request_counter = 0
        self._lock = asyncio.Lock()
        
    async def process_alert(self, alert_data: AlertData) -> TradeResponse:
        """Process an alert and execute appropriate trading action"""
        request_id = str(uuid.uuid4())
        logger.info(f"[{request_id}] Processing alert: {alert_data.model_dump_json()}")
        
        try:
            # Increment request counter
            async with self._lock:
                self.request_counter += 1
                
            # Extract basic info
            action = alert_data.action.upper()
            instrument = alert_data.symbol
            
            # Handle position closure
            if action == 'CLOSE':
                logger.info(f"[{request_id}] Closing position for {instrument}")
                result = await close_position(instrument)
                
                return TradeResponse(
                    success=True,
                    message=f"Position closed for {instrument}",
                    request_id=request_id,
                    trade_id=result.get('lastTransactionID'),
                    details=result
                )
                
            # Handle BOS/CHoCH signals
            is_bos = alert_data.signalType == 'BOS' or 'BOS' in action
            is_choch = alert_data.signalType == 'CHOCH' or 'CHOCH' in action
            
            # Skip if disabled in config
            if (is_bos and not config.bos_enable) or (is_choch and not config.choch_enable):
                logger.info(f"[{request_id}] Signal type {alert_data.signalType} is disabled in config")
                return TradeResponse(
                    success=False,
                    message=f"Signal type {alert_data.signalType} is disabled in config",
                    request_id=request_id
                )
            
            # Determine trade direction and size
            is_buy = 'BUY' in action
            units = await calculate_position_size(
                instrument, 
                alert_data.percentage or config.risk_percentage
            )
            
            if not is_buy:
                units = -units  # Negative units for sell orders
            
            # Execute market order without SL/TP
            result = await execute_market_order(
                instrument,
                units
            )
            
            return TradeResponse(
                success=True,
                message=f"Order executed for {instrument}",
                request_id=request_id,
                trade_id=result.get('lastTransactionID'),
                details=result
            )
                
        except Exception as e:
            logger.error(f"[{request_id}] Error processing alert: {str(e)}")
            return TradeResponse(
                success=False,
                message=f"Error: {str(e)}",
                request_id=request_id
            )

##############################################################################
# API Endpoints
##############################################################################

# Initialize FastAPI with lifespan management
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Initialize components on startup
    alert_handler = AlertHandler()
    app.state.alert_handler = alert_handler
    
    logger.info("Application started")
    yield
    
    # Cleanup on shutdown
    await cleanup_sessions()
    logger.info("Application shutdown complete")

app = FastAPI(
    title="TradingView to Oanda Trading Bot",
    description="Processes TradingView webhook alerts for Smart Money Concepts strategy and executes trades on Oanda",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in config.allowed_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def request_middleware(request: Request, call_next):
    """Add request ID and timing to all requests"""
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    
    logger.info(f"[{request_id}] Request: {request.method} {request.url.path}")
    start_time = time.time()
    
    # Set the request ID in the logger context
    logger_adapter = logging.LoggerAdapter(
        logger, 
        {'request_id': request_id}
    )
    
    # Store logger adapter in request state
    request.state.logger = logger_adapter
    
    response = await call_next(request)
    
    # Log request completion
    duration = time.time() - start_time
    logger.info(f"[{request_id}] Response: {response.status_code} - Took {duration:.3f}s")
    
    # Add request ID to response headers
    response.headers["X-Request-ID"] = request_id
    
    return response

def get_alert_handler(request: Request) -> AlertHandler:
    """Dependency to get alert handler from app state"""
    return request.app.state.alert_handler

@app.post("/webhook")
async def process_tradingview_webhook(
    request: Request,
    alert_handler: AlertHandler = Depends(get_alert_handler)
) -> TradeResponse:
    """Process webhook from TradingView"""
    request_id = request.state.request_id
    logger = request.state.logger

    try:
        body = await request.json()
        logger.info(f"Received webhook data: {json.dumps(body)}")
        
        # Convert TradingView format to our alert format
        alert_data = {}
        
        for our_field, tv_field in TV_FIELD_MAP.items():
            if tv_field in body:
                alert_data[our_field] = body[tv_field]
        
        # Check for BOS/CHoCH in the message if not explicitly set
        message = body.get('message', '').upper()
        if 'signalType' not in alert_data:
            if 'BOS' in message:
                alert_data['signalType'] = 'BOS'
            elif 'CHOCH' in message:
                alert_data['signalType'] = 'CHOCH'
                
        # Set action if not present based on message
        if 'action' not in alert_data:
            if 'BUY' in message:
                alert_data['action'] = 'BUY'
            elif 'SELL' in message:
                alert_data['action'] = 'SELL'
        
        # Create an AlertData instance
        validated_alert = AlertData(**alert_data)
        
        # Process the alert
        return await alert_handler.process_alert(validated_alert)
        
    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook request")
        raise HTTPException(
            status_code=400, 
            detail="Invalid JSON format"
        )
    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")
        return TradeResponse(
            success=False,
            message=f"Error: {str(e)}",
            request_id=request_id
        )

@app.post("/alerts")
async def process_alert(
    alert: AlertData,
    request: Request,
    alert_handler: AlertHandler = Depends(get_alert_handler)
) -> TradeResponse:
    """Process alert from direct API call"""
    request_id = request.state.request_id
    logger = request.state.logger
    
    logger.info(f"Processing direct alert: {alert.model_dump_json()}")
    
    # Process the alert
    return await alert_handler.process_alert(alert)

@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring"""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone(config.timezone)).isoformat(),
        "version": "1.0.0"
    }

@app.get("/positions")
async def get_positions():
    """Get current open positions"""
    try:
        positions = await get_open_positions()
        return positions
    except Exception as e:
        logger.error(f"Error fetching positions: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching positions: {str(e)}"
        )

@app.get("/account")
async def get_account():
    """Get account summary"""
    try:
        account = await get_account_summary()
        return account
    except Exception as e:
        logger.error(f"Error fetching account summary: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching account summary: {str(e)}"
        )

##############################################################################
# Main Entry Point
##############################################################################

if __name__ == "__main__":
    import uvicorn
    
    # Get port from environment with fallback
    port = int(os.environ.get("PORT", 8000))
    
    logger.info(f"Starting server on port {port}")
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_config=None,  # We've already configured logging
        timeout_keep_alive=65,
        workers=1
    )
