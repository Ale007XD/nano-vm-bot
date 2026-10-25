"""
Example system skill: Calculator
Demonstrates basic skill structure
"""

import math

SKILL_NAME = "calculator"
SKILL_DESCRIPTION = "Perform mathematical calculations"
SKILL_CATEGORY = "utility"
SKILL_VERSION = "1.0.0"
SKILL_AUTHOR = "Alex-Nano-Bot System"
SKILL_COMMANDS = ["/calc"]


async def handle_command(command: str, args: list, message, bot):
    """Handle calculator commands"""
    if command == "calc":
        return await calculate(message, args)
    return None


async def calculate(message, args):
    """Perform calculation from user input"""
    if not args:
        await message.reply(
            "🧮 <b>Calculator</b>\n\n"
            "Usage: <code>/calc 2 + 2</code>\n"
            "Supports: +, -, *, /, **, parentheses\n"
            "Functions: sqrt, sin, cos, tan, log, exp"
        )
        return
    
    expression = " ".join(args)
    
    try:
        # Clean and validate expression
        result = evaluate_expression(expression)
        
        await message.reply(
            f"🧮 <b>Calculation</b>\n\n"
            f"<code>{expression}</code> = <b>{result}</b>"
        )
        
    except Exception as e:
        await message.reply(
            f"❌ <b>Calculation Error</b>\n\n"
            f"Could not evaluate: <code>{expression}</code>\n"
            f"Error: {str(e)}"
        )


def evaluate_expression(expr: str) -> float:
    """Safely evaluate mathematical expression"""
    # Remove any non-math characters for security
    allowed_chars = set('0123456789+-*/.()^ sqrtcosintaegl')
    if not all(c in allowed_chars for c in expr.replace(' ', '')):
        raise ValueError("Invalid characters in expression")
    
    # Replace ^ with **
    expr = expr.replace('^', '**')
    
    # Create safe namespace with math functions
    safe_dict = {
        'sqrt': math.sqrt,
        'sin': math.sin,
        'cos': math.cos,
        'tan': math.tan,
        'log': math.log,
        'exp': math.exp,
        'pi': math.pi,
        'e': math.e
    }
    
    try:
        result = eval(expr, {"__builtins__": {}}, safe_dict)
        return round(result, 10)  # Round to avoid floating point errors
    except Exception as e:
        raise ValueError(f"Invalid expression: {e}")


def setup_handlers():
    """Setup command handlers for this skill"""
    return {
        "calc": handle_command
    }
