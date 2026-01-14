 21544from flask import Flask, request, jsonify



app = Flask(__name__)



@app.route("/")

def home():

    return "MME AI Bot is running"



@app.route("/estimate", methods=["POST"])

def estimate():

    data = request.json

    service = data.get("service")

    details = data.get("details")



    # Placeholder pricing logic

    price_range = "$100 - $300"



    return jsonify({

        "service": service,

        "estimated_range": price_range,

        "message": "This is a rough estimate. Final pricing requires inspection."

    })



if __name__ == "__main__":

    app.run(host="0.0.0.0", port=3