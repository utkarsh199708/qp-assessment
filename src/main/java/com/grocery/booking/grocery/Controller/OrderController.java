package com.grocery.booking.grocery.Controller;

import com.grocery.booking.grocery.DTO.OrderItemRequest;
import com.grocery.booking.grocery.Entity.GroceryItem;
import com.grocery.booking.grocery.Entity.Order;
import com.grocery.booking.grocery.Service.GroceryItemService;
import com.grocery.booking.grocery.Service.OrderService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/user/grocery")
public class OrderController {
    @Autowired
    private GroceryItemService groceryItemService;
    @Autowired
    private OrderService orderService;

    // Endpoint to view the list of available grocery items
    @GetMapping
    public List<GroceryItem> getAllAvailableGroceryItems() {
        return groceryItemService.getAllAvailableGroceryItems();
    }

    // Endpoint to book multiple grocery items in a single order
    @PostMapping("/order")
    public ResponseEntity<Order> createOrder(@RequestBody List<OrderItemRequest> orderItems) {
        Order order = orderService.createOrder(orderItems);
        return new ResponseEntity<>(order, HttpStatus.CREATED);
    }
}