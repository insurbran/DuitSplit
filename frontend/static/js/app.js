const mockItems = [
    { id: 1, name: "Chicken Rice", price: 12.00 },
    { id: 2, name: "Iced Tea", price: 3.50 },
    { id: 3, name: "Nasi Lemak", price: 10.00 },
    { id: 4, name: "Service Tax", price: 2.50 }
];

let friends = [];
let assignments = {};

const receiptInput = document.getElementById("receiptInput");
const receiptPreview = document.getElementById("receiptPreview");
const processReceiptBtn = document.getElementById("processReceiptBtn");
const itemsSection = document.getElementById("itemsSection");
const friendsSection = document.getElementById("friendsSection");
const summarySection = document.getElementById("summarySection");
const itemsList = document.getElementById("itemsList");
const friendNameInput = document.getElementById("friendNameInput");
const addFriendBtn = document.getElementById("addFriendBtn");
const friendsList = document.getElementById("friendsList");
const summaryList = document.getElementById("summaryList");

receiptInput.addEventListener("change", () => {
    const file = receiptInput.files[0];

    if (!file) {
        return;
    }

    receiptPreview.src = URL.createObjectURL(file);
    receiptPreview.classList.remove("d-none");
});

processReceiptBtn.addEventListener("click", () => {
    const aiStatus = document.getElementById("aiStatus");

    if (!receiptInput.files[0]) {
        alert("Please upload a receipt first.");
        return;
    }

    aiStatus.textContent = "🤖 AI is processing the receipt...";
    aiStatus.className = "text-warning mb-0";

    itemsSection.classList.remove("d-none");
    friendsSection.classList.remove("d-none");

    setTimeout(() => {
        aiStatus.textContent = "✅ Receipt processed successfully!";
        aiStatus.className = "text-success mb-0";

        renderItems();
    }, 1000);
});

addFriendBtn.addEventListener("click", () => {
    const name = friendNameInput.value.trim();

    if (!name) {
        alert("Please enter a friend name.");
        return;
    }

    friends.push(name);
    friendNameInput.value = "";

    renderFriends();
    renderItems();
    calculateSummary();
});

function renderFriends() {
    friendsList.innerHTML = friends
        .map(friend => `<span class="badge text-bg-secondary me-2">${friend}</span>`)
        .join("");
}

function renderItems() {
    itemsList.innerHTML = mockItems.map(item => {
        const checkboxes = friends.map(friend => {
            const checked = assignments[item.id]?.includes(friend) ? "checked" : "";

            return `
                <div class="form-check form-check-inline">
                    <input class="form-check-input assignment-checkbox"
                        type="checkbox"
                        data-item-id="${item.id}"
                        data-friend="${friend}"
                        ${checked}>
                    <label class="form-check-label">${friend}</label>
                </div>
            `;
        }).join("");

        return `
            <div class="item-card">
                <div class="d-flex justify-content-between">
                    <strong>${item.name}</strong>
                    <span>RM ${item.price.toFixed(2)}</span>
                </div>

                <div class="mt-2">
                    ${friends.length ? checkboxes : "<small class='text-muted'>Add friends first to assign this item.</small>"}
                </div>
            </div>
        `;
    }).join("");

    document.querySelectorAll(".assignment-checkbox").forEach(checkbox => {
        checkbox.addEventListener("change", handleAssignmentChange);
    });
}

function handleAssignmentChange(event) {
    const itemId = Number(event.target.dataset.itemId);
    const friend = event.target.dataset.friend;

    if (!assignments[itemId]) {
        assignments[itemId] = [];
    }

    if (event.target.checked) {
        assignments[itemId].push(friend);
    } else {
        assignments[itemId] = assignments[itemId].filter(name => name !== friend);
    }

    calculateSummary();
}

function calculateSummary() {
    const totals = {};

    friends.forEach(friend => {
        totals[friend] = 0;
    });

    mockItems.forEach(item => {
        const assignedFriends = assignments[item.id] || [];

        if (assignedFriends.length === 0) {
            return;
        }

        const splitAmount = item.price / assignedFriends.length;

        assignedFriends.forEach(friend => {
            totals[friend] += splitAmount;
        });
    });

    summarySection.classList.remove("d-none");

    summaryList.innerHTML = Object.entries(totals)
        .map(([friend, total]) => `
            <div class="d-flex justify-content-between border-bottom py-2">
                <span>${friend}</span>
                <strong>RM ${total.toFixed(2)}</strong>
            </div>
        `)
        .join("");
}